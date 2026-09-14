"""The REST surface around the MCP server: login, validation, practice admin.

This exists so a practice can connect itself without anyone touching a config file,
and so the MyOTGO app (or any other product) can drive the whole onboarding from its
own UI: send the user to `/auth/login`, wait for the callback, then start calling
tools with `X-Dentally-Practice` set.

Two ways in, both supported on purpose:

* `GET /auth/login` → the full OAuth2 authorization-code flow. Right for a
  multi-practice product; needs a Dentally OAuth application.
* `POST /auth/token` → a practice pastes the API token it generated inside Dentally.
  We validate it against `/v1/user` before storing it, so a typo fails here rather
  than mysteriously at the first tool call.

Everything except `/healthz`, `/auth/login` and `/auth/callback` requires the
server's own bearer token. Practice tokens are never returned by any endpoint.
"""
from __future__ import annotations

import html
import json
import logging

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, config
from .errors import DentallyError
from .runtime import FLOW, STORE

log = logging.getLogger("dentally_mcp.http")


def _esc(value: object) -> str:
    """Escape untrusted text for HTML. Everything not literal in this file is untrusted."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _html_headers(*, connect_page: bool = False) -> dict[str, str]:
    """Security headers for the HTML pages.

    The connect page collects two live credentials, so a script injected into it
    would be a credential thief. The CSP is the backstop for that: even if an
    escaping bug slipped back in, injected script has no way to run or phone home.

    `unsafe-inline` is present only because the page's own script and style are
    inline; it is scoped by `default-src 'none'`, so there is still no path to an
    external origin. frame-ancestors 'none' stops the credential form being framed
    and clickjacked.
    """
    script = "'unsafe-inline'" if connect_page else "'none'"
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
            "form-action 'none'; frame-ancestors 'none'; base-uri 'none'; connect-src 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        # These pages handle credentials; a shared cache holding one would be a leak.
        "Cache-Control": "no-store",
    }


def _guard(request):
    """Admin endpoints need the server's own bearer token, not Dentally's."""
    if not auth.verify_client(request.headers.get("authorization")):
        # Log the rejection. Without this, someone probing for the admin token leaves
        # no trace at all, and the first sign of a problem is a connected practice
        # nobody added. The token itself is never logged.
        log.warning("Rejected unauthenticated request to %s from %s",
                    request.url.path,
                    request.client.host if request.client else "unknown")
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": 'Bearer realm="dentally-mcp"'})
    return None


def _error(exc: Exception, status: int = 400) -> JSONResponse:
    """Return our own message, never Dentally's raw response body.

    `DentallyError.detail` holds the upstream body verbatim. On a 422 that body
    echoes back the fields we submitted — which for `register_patient` is a name and
    a date of birth. Forwarding it turned every validation failure into a small
    patient-data disclosure to whoever called the endpoint. The detail stays on the
    exception for server-side logging; it does not cross the wire.
    """
    if isinstance(exc, DentallyError):
        return JSONResponse(
            {"error": exc.message, "status": exc.status},
            status_code=exc.status or status,
        )
    return JSONResponse({"error": str(exc)}, status_code=status)


async def healthz(request):
    """Liveness. Configuration detail only for an authenticated caller.

    This endpoint has to stay public so a load balancer can probe it, but the full
    body was reconnaissance for anyone who found the URL: whether writes are on,
    whether redaction is off, and how many practices are connected together describe
    exactly how rewarding an attack would be. The unauthenticated answer is now
    liveness plus the demo flag — the demo flag stays public precisely because
    mistaking synthetic data for a real diary is the failure it prevents.
    """
    public = {"status": "ok", "demo_mode": config.DEMO}
    if not auth.verify_client(request.headers.get("authorization")):
        return JSONResponse(public)

    return JSONResponse({
        **public,
        "region": config.REGION,
        "api_base": config.API_BASE,
        "environment": "sandbox" if config.IS_SANDBOX else "production",
        "auth_mode": ("DEMO — synthetic data, not a real practice" if config.DEMO
                      else "api_token" if config.API_TOKEN
                      else "oauth" if config.CLIENT_ID else "unconfigured"),
        "writes_enabled": config.ALLOW_WRITES,
        "pii_redacted": config.REDACT_PII,
        "practices_connected": len(STORE.list_ids()) if config.TOKEN_STORE_KEY else 0,
    })


async def auth_login(request):
    """Start the OAuth flow. `?json=1` returns the URL instead of redirecting."""
    try:
        url, state = FLOW.authorize_url(practice_hint=request.query_params.get("practice"))
    except DentallyError as exc:
        return _error(exc)
    if request.query_params.get("json"):
        return JSONResponse({"authorize_url": url, "state": state})
    return RedirectResponse(url, status_code=302)


async def auth_callback(request):
    """Dentally redirects here with ?code & ?state. Exchange and store."""
    error = request.query_params.get("error")
    if error:
        # `error` is a raw query parameter on an UNAUTHENTICATED endpoint. Escaping
        # it is the whole fix for the reflected XSS this line used to be.
        return HTMLResponse(_page("Authorisation refused", f"Dentally reported: {_esc(error)}"),
                            status_code=400, headers=_html_headers())

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return HTMLResponse(_page("Missing details", "The callback had no code or state."),
                            status_code=400, headers=_html_headers())

    try:
        token = await FLOW.exchange(code, state)
    except DentallyError as exc:
        log.warning("OAuth exchange failed: %s", exc.message)
        # An error message can carry text Dentally sent us; treat it as untrusted.
        return HTMLResponse(_page("Could not complete sign-in", _esc(exc.message)),
                            status_code=400, headers=_html_headers())

    # The practice name comes back from Dentally's API, so it is untrusted input
    # even though the source is a system we authenticated to.
    return HTMLResponse(_page(
        "Connected to Dentally",
        f"Practice <b>{_esc(token.practice_name or token.practice_id)}</b> is now connected. "
        f"Use <code>X-Dentally-Practice: {_esc(token.practice_id)}</code> when calling the MCP server."
        "<br><br>You can close this window.",
    ), headers=_html_headers())


async def auth_token(request):
    """Register a practice-generated Dentally API token after proving it works."""
    denied = _guard(request)
    if denied:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"error": "field 'token' is required"}, status_code=400)

    try:
        stored = await FLOW.register_api_token(token, label=body.get("label"))
    except DentallyError as exc:
        return _error(exc, 401)
    return JSONResponse({"connected": stored.redacted()})


async def auth_validate(request):
    """Check a token against Dentally and report what it can do. Stores nothing."""
    denied = _guard(request)
    if denied:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    token = (body.get("token") or "").strip() or config.API_TOKEN
    if not token:
        return JSONResponse({"error": "no token supplied and DENTALLY_API_TOKEN is not set"}, status_code=400)

    try:
        identity = await FLOW.identify(token)
    except DentallyError as exc:
        return _error(exc, 401)
    return JSONResponse({"valid": True, **identity})


async def list_practices(request):
    denied = _guard(request)
    if denied:
        return denied
    if not config.TOKEN_STORE_KEY:
        return JSONResponse({"practices": [], "note": "DENTALLY_TOKEN_KEY not set; no store."})
    return JSONResponse({"practices": [t.redacted() for t in STORE.all()]})


async def revoke_practice(request):
    """Forget a practice's credential. The practice should also revoke it in Dentally."""
    denied = _guard(request)
    if denied:
        return denied
    practice_id = request.path_params["practice_id"]
    removed = STORE.delete(practice_id)
    return JSONResponse({"removed": removed, "practice_id": practice_id})


async def run_keepalive(request):
    """Ping every stored token now.

    Exposed as an endpoint, not just a background loop, so an operator can prove the
    tokens are alive rather than assume it — and so an external scheduler can drive it.
    """
    denied = _guard(request)
    if denied:
        return denied
    return JSONResponse({"results": await FLOW.keepalive_once()})


async def connect_page(request):
    """A browser form for connecting a practice, instead of hand-built curl.

    The people who will actually do this are practice managers, not engineers — and
    even for an engineer, the curl for it is a footgun on Windows, where PowerShell
    aliases `curl` to Invoke-WebRequest and silently rejects every flag.

    The page posts to `/auth/token`, which already validates the credential against
    Dentally before storing it. Nothing new is trusted here; this is a front end for
    an endpoint that already exists.

    It asks for the server's admin token in a field rather than embedding it, so the
    page itself grants nothing and is safe to leave reachable.
    """
    return HTMLResponse(_CONNECT_HTML, headers=_html_headers(connect_page=True))


async def protected_resource_metadata(request):
    """RFC 9728 metadata so MCP clients can discover how to authorise."""
    return JSONResponse({
        "resource": f"{config.PUBLIC_URL}/mcp",
        "authorization_servers": [config.PUBLIC_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": config.SCOPES.split(),
        "resource_documentation": "https://github.com/keyhan-azarjoo/dentally-mcp",
    })


def routes() -> list[Route]:
    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/auth/login", auth_login, methods=["GET"]),
        Route("/auth/callback", auth_callback, methods=["GET"]),
        Route("/auth/token", auth_token, methods=["POST"]),
        Route("/auth/validate", auth_validate, methods=["POST"]),
        Route("/auth/practices", list_practices, methods=["GET"]),
        Route("/auth/practices/{practice_id}", revoke_practice, methods=["DELETE"]),
        Route("/auth/keepalive", run_keepalive, methods=["POST"]),
        Route("/connect", connect_page, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    ]


_CONNECT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect a practice — Dentally MCP</title>
<style>
 :root { color-scheme: light dark; }
 body { font-family: -apple-system, system-ui, Segoe UI, sans-serif; max-width: 34rem;
        margin: 6vh auto; padding: 0 1.5rem; line-height: 1.6; }
 h1 { font-size: 1.5rem; margin-bottom: .25rem; }
 p.sub { margin-top: 0; opacity: .75; }
 label { display: block; margin: 1.1rem 0 .3rem; font-weight: 600; font-size: .92rem; }
 input { width: 100%; padding: .6rem .7rem; font-size: 1rem; border-radius: .4rem;
         border: 1px solid #99a; background: transparent; color: inherit; font-family: inherit; }
 button { margin-top: 1.4rem; padding: .65rem 1.3rem; font-size: 1rem; font-weight: 600;
          border: 0; border-radius: .4rem; background: #1a6acb; color: #fff; cursor: pointer; }
 button:disabled { opacity: .5; cursor: default; }
 .hint { font-size: .85rem; opacity: .75; margin-top: .3rem; }
 #out { margin-top: 1.5rem; padding: .9rem 1rem; border-radius: .4rem; display: none;
        white-space: pre-wrap; font-size: .9rem; }
 .ok { background: #e6f5ec; color: #14532d; }
 .err { background: #fdeaea; color: #7f1d1d; }
 code { background: #0001; padding: .1rem .35rem; border-radius: .25rem; }
</style></head>
<body>
<h1>Connect a practice</h1>
<p class="sub">Links a Dentally practice to this MCP server.</p>

<form id="f">
  <label for="admin">Server admin token</label>
  <input id="admin" type="password" autocomplete="off" placeholder="the DENTALLY_MCP_AUTH_TOKEN for this server">
  <div class="hint">Not your Dentally password. This is the token your server was configured with.</div>

  <label for="tok">Dentally API token</label>
  <input id="tok" type="password" autocomplete="off" placeholder="generated in Dentally">
  <div class="hint">In Dentally: <b>Settings &rarr; Developer Settings &rarr; Generate new token</b>.
     Scope it to only what you need &mdash; <code>patient:read</code>, <code>appointment:read</code>.</div>

  <label for="label">Practice name (optional)</label>
  <input id="label" type="text" placeholder="e.g. Smile Dental, Bristol">

  <button type="submit" id="go">Check and connect</button>
</form>

<div id="out"></div>

<script>
const f = document.getElementById('f'), out = document.getElementById('out'), go = document.getElementById('go');
f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const admin = document.getElementById('admin').value.trim();
  const token = document.getElementById('tok').value.trim();
  if (!admin || !token) { show('err', 'Both tokens are required.'); return; }

  go.disabled = true; go.textContent = 'Checking with Dentally…';
  try {
    const r = await fetch('/auth/token', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + admin, 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, label: document.getElementById('label').value.trim() || null })
    });
    const body = await r.json();
    if (!r.ok) {
      // The server already distinguishes "your admin token is wrong" from "Dentally
      // rejected the practice token"; surfacing that saves the usual guessing.
      show('err', (r.status === 401 && !body.error?.includes('Dentally'))
        ? 'The server admin token was not accepted.'
        : (body.error || ('Failed with HTTP ' + r.status)));
      return;
    }
    const p = body.connected || {};
    show('ok', 'Connected.\\n\\nPractice: ' + (p.practice_name || p.practice_id) +
              '\\nPractice ID: ' + p.practice_id +
              '\\nScopes: ' + ((p.scopes || []).join(' ') || '(not reported)') +
              '\\n\\nUse this header when calling the MCP server:\\n' +
              'X-Dentally-Practice: ' + p.practice_id);
    // Do not leave a live credential sitting in a form field.
    document.getElementById('tok').value = '';
  } catch (err) {
    show('err', 'Could not reach the server: ' + err);
  } finally {
    go.disabled = false; go.textContent = 'Check and connect';
  }
});
function show(cls, msg) { out.className = cls; out.textContent = msg; out.style.display = 'block'; }
</script>
</body></html>"""


def _page(title: str, body_html: str) -> str:
    """Render a small status page.

    `body_html` is TRUSTED MARKUP — every caller must escape anything that did not
    come from this file. Use `_esc()` on it. The parameter is named `body_html`
    rather than `body` so that obligation is visible at each call site: an earlier
    version took `body` and one caller passed a query parameter straight through,
    which was a reflected XSS on an unauthenticated endpoint.
    """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 34rem;
        margin: 12vh auto; padding: 0 1.5rem; color: #16222c; line-height: 1.6; }}
 h1 {{ font-size: 1.4rem; margin-bottom: .5rem; }}
 code {{ background: #eef2f5; padding: .1rem .35rem; border-radius: .25rem; }}
</style></head>
<body><h1>{html.escape(title)}</h1><p>{body_html}</p></body></html>"""
