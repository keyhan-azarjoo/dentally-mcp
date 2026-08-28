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

import json
import logging

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, config
from .errors import DentallyError
from .runtime import FLOW, STORE

log = logging.getLogger("dentally_mcp.http")


def _guard(request):
    """Admin endpoints need the server's own bearer token, not Dentally's."""
    if not auth.verify_client(request.headers.get("authorization")):
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": 'Bearer realm="dentally-mcp"'})
    return None


def _error(exc: Exception, status: int = 400) -> JSONResponse:
    if isinstance(exc, DentallyError):
        return JSONResponse(
            {"error": exc.message, "status": exc.status, "detail": exc.detail},
            status_code=exc.status or status,
        )
    return JSONResponse({"error": str(exc)}, status_code=status)


async def healthz(request):
    """Liveness plus enough configuration truth to debug a bad deploy."""
    return JSONResponse({
        "status": "ok",
        "region": config.REGION,
        "api_base": config.API_BASE,
        "environment": "sandbox" if config.IS_SANDBOX else "production",
        "auth_mode": "api_token" if config.API_TOKEN else ("oauth" if config.CLIENT_ID else "unconfigured"),
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
        return HTMLResponse(_page("Authorisation refused", f"Dentally reported: {error}"), status_code=400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return HTMLResponse(_page("Missing details", "The callback had no code or state."), status_code=400)

    try:
        token = await FLOW.exchange(code, state)
    except DentallyError as exc:
        log.warning("OAuth exchange failed: %s", exc.message)
        return HTMLResponse(_page("Could not complete sign-in", exc.message), status_code=400)

    return HTMLResponse(_page(
        "Connected to Dentally",
        f"Practice <b>{token.practice_name or token.practice_id}</b> is now connected. "
        f"Use <code>X-Dentally-Practice: {token.practice_id}</code> when calling the MCP server."
        "<br><br>You can close this window.",
    ))


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
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    ]


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 34rem;
        margin: 12vh auto; padding: 0 1.5rem; color: #16222c; line-height: 1.6; }}
 h1 {{ font-size: 1.4rem; margin-bottom: .5rem; }}
 code {{ background: #eef2f5; padding: .1rem .35rem; border-radius: .25rem; }}
</style></head>
<body><h1>{title}</h1><p>{body}</p></body></html>"""
