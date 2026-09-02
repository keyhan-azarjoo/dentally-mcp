"""Runtime configuration, all from environment variables.

Nothing here reads a file or a secret store: the process is configured entirely by
env so the same image runs under Docker, launchd, or `uvx` on a clinician's laptop.
"""
from __future__ import annotations

import os

from . import __version__


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- Which Dentally instance we talk to --------------------------------------
# Dentally is regionally sharded and the sandbox is a SEPARATE host with separate
# credentials — a UK production token is not valid against the sandbox and vice
# versa. Selecting by region name (rather than making everyone paste a URL) keeps
# people from silently pointing production tools at sandbox data.
REGIONS: dict[str, str] = {
    "uk": "https://api.dentally.co",
    "sandbox": "https://api.sandbox.dentally.co",
    "apac": "https://api.apac.dentally.com",
    "ca": "https://api.ca.dentally.com",
}

REGION = os.environ.get("DENTALLY_REGION", "sandbox").strip().lower()
# An explicit base URL always wins, so a partner given a bespoke host is not blocked
# on us shipping a new release.
API_BASE = (os.environ.get("DENTALLY_API_BASE") or REGIONS.get(REGION) or REGIONS["sandbox"]).rstrip("/")
API_VERSION = os.environ.get("DENTALLY_API_VERSION", "v1").strip().strip("/")

IS_SANDBOX = "sandbox" in API_BASE

# --- Credentials -------------------------------------------------------------
# Mode A: a single practice's own API token (Settings -> Integrations -> API).
API_TOKEN = os.environ.get("DENTALLY_API_TOKEN", "").strip()

# Mode C: DEMO — a synthetic Dentally, no credential of any kind.
#
# Getting a real token needs an admin account and the right licence, and the sandbox
# needs partner approval. Without this, anyone evaluating the server — or a nurse who
# wants to show their practice manager what it does before asking for access — has
# nothing to run. The fake is the *upstream* only: rate limiting, pagination,
# redaction and role scoping are all the real code path.
DEMO = _flag("DENTALLY_DEMO", False)

# Mode B: OAuth2 authorization-code, for a multi-practice integration. The endpoint
# paths are configurable because Dentally hands the exact authorize/token URLs to
# each partner at onboarding; the defaults are the conventional ones.
CLIENT_ID = os.environ.get("DENTALLY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("DENTALLY_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get("DENTALLY_REDIRECT_URI", "").strip()
AUTHORIZE_URL = os.environ.get("DENTALLY_AUTHORIZE_URL", f"{API_BASE}/oauth/authorize")
TOKEN_URL = os.environ.get("DENTALLY_TOKEN_URL", f"{API_BASE}/oauth/token")
# Space-separated. Ask for the least you need — the practice sees this list on the
# consent screen and an over-broad ask is a common reason partner review stalls.
SCOPES = os.environ.get("DENTALLY_SCOPES", "user:read patient:read appointment:read").strip()

# --- Our own HTTP surface ----------------------------------------------------
HOST = os.environ.get("DENTALLY_MCP_HOST", "0.0.0.0")
PORT = int(_num("DENTALLY_MCP_PORT", 8092))
# Public URL this server is reachable at. Used to build the OAuth redirect and the
# MCP resource metadata that remote clients (OpenAI, Claude web) discover.
PUBLIC_URL = os.environ.get("DENTALLY_MCP_PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")

# Bearer token that OUR clients (the MyOTGO backend, an OpenAI Responses call, a
# Claude connector) must present to use the HTTP transport. Empty disables the HTTP
# transport's auth gate, which is only ever acceptable on a loopback bind.
CLIENT_AUTH_TOKEN = os.environ.get("DENTALLY_MCP_AUTH_TOKEN", "").strip()


def _allowed_hosts() -> list[str]:
    """Host header allow-list for DNS-rebinding protection.

    FastMCP only switches this on by itself when bound to loopback. Behind a reverse
    proxy — which is every real deployment — it binds 0.0.0.0 and the protection
    silently does not apply. So we build the list ourselves from the public URL.

    Both the bare host and a `:*` port wildcard are included: a proxy may or may not
    pass the port through in the Host header, and getting that wrong rejects every
    request with an opaque 400.
    """
    explicit = os.environ.get("DENTALLY_MCP_ALLOWED_HOSTS", "").strip()
    if explicit:
        raw = [h.strip() for h in explicit.replace(",", " ").split() if h.strip()]
    else:
        from urllib.parse import urlparse

        host = urlparse(PUBLIC_URL).hostname or "localhost"
        raw = [host, "localhost", "127.0.0.1"]
    out: list[str] = []
    for host in raw:
        out.append(host)
        if ":*" not in host:
            out.append(f"{host}:*")
    return out


ALLOWED_HOSTS = _allowed_hosts()
# Browsers send Origin; native clients (Claude Desktop, an OpenAI backend call) do not,
# and an absent Origin is treated as same-origin and allowed.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("DENTALLY_MCP_ALLOWED_ORIGINS", PUBLIC_URL).replace(",", " ").split()
    if o.strip()
]

# --- Safety ------------------------------------------------------------------
# Writes (book/cancel/register/edit) are OFF by default. A read-only assistant that
# gets a prompt-injected instruction can leak; one with writes can double-book a
# surgery or delete a patient. Turning this on is a deliberate, auditable act.
ALLOW_WRITES = _flag("DENTALLY_ALLOW_WRITES", False)
# Ask the human to confirm each write via MCP elicitation. Fails CLOSED when the
# client cannot elicit, so a non-interactive client can never write silently.
REQUIRE_CONSENT = _flag("DENTALLY_REQUIRE_CONSENT", True)
CONSENT_FALLBACK_ALLOW = _flag("DENTALLY_CONSENT_FALLBACK_ALLOW", False)

# Minimise what reaches the model. Contact details and dates of birth are not needed
# to answer "who is in surgery 2 this afternoon", and UK dental records are special
# category data under UK GDPR Art. 9.
REDACT_PII = _flag("DENTALLY_REDACT_PII", True)
# Tooth/surface-level clinical detail is never exposed, at any setting. Dentally's
# own integration guidance singles it out as data that should not leave the system,
# and no scheduling or billing question needs it.
BLOCK_CLINICAL_DETAIL = True

AUDIT_LOG = os.environ.get("DENTALLY_AUDIT_LOG", "").strip()

# Which role surface a caller gets when it does not ask for one.
DEFAULT_SURFACE = os.environ.get("DENTALLY_DEFAULT_SURFACE", "reception").strip().lower()

# --- HTTP client behaviour ---------------------------------------------------
# Dentally REJECTS any request without a User-Agent with 403 (not 401), which reads
# like a permissions problem and has cost integrators hours. It is never optional.
USER_AGENT = os.environ.get(
    "DENTALLY_USER_AGENT",
    f"dentally-mcp/{__version__} (+https://github.com/keyhan-azarjoo/dentally-mcp)",
)

TIMEOUT = _num("DENTALLY_TIMEOUT", 30.0)
CONNECT_TIMEOUT = _num("DENTALLY_CONNECT_TIMEOUT", 5.0)
GET_RETRIES = int(_num("DENTALLY_GET_RETRIES", 2))
RETRY_BACKOFF = _num("DENTALLY_RETRY_BACKOFF", 0.25)

# Documented ceiling: 3,600 requests/hour/user. We self-limit slightly under it so a
# burst never costs the practice its whole hour.
RATE_LIMIT_PER_HOUR = int(_num("DENTALLY_RATE_LIMIT_PER_HOUR", 3400))
# Dentally caps page size at 100 and warns that paging beyond page 100 times out.
MAX_PER_PAGE = 100
MAX_PAGES = int(_num("DENTALLY_MAX_PAGES", 20))
# Their guidance: keep date filters on large entities inside ~3 months.
MAX_DATE_WINDOW_DAYS = int(_num("DENTALLY_MAX_DATE_WINDOW_DAYS", 92))

# Dentally issues tokens with NO refresh mechanism — a token stays valid by being
# used and dies when it goes idle. This background ping is what keeps a long-lived
# integration alive; disabling it means re-authorising every practice by hand.
KEEPALIVE_ENABLED = _flag("DENTALLY_KEEPALIVE", True)
KEEPALIVE_INTERVAL_HOURS = _num("DENTALLY_KEEPALIVE_HOURS", 12.0)

TOKEN_STORE_PATH = os.environ.get("DENTALLY_TOKEN_STORE", "tokens.enc")
# Fernet key for tokens at rest. Generate with `python -m dentally_mcp.tokenstore --genkey`.
TOKEN_STORE_KEY = os.environ.get("DENTALLY_TOKEN_KEY", "").strip()

SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", "").strip() or ("sandbox" if IS_SANDBOX else "production")


def api_url(path: str) -> str:
    """Build a fully versioned API URL from a bare resource path."""
    path = path.lstrip("/")
    if path.startswith(f"{API_VERSION}/"):
        return f"{API_BASE}/{path}"
    return f"{API_BASE}/{API_VERSION}/{path}"
