"""The Dentally MCP server: stdio for local clients, Streamable HTTP for hosted ones.

Both transports serve the same tools through the same guards. The difference is only
how the caller is identified — stdio trusts whoever launched the process, HTTP
requires the server's own bearer token and a named practice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import auth, config, http_api, surfaces, tools
from .runtime import FLOW

logging.basicConfig(
    level=os.environ.get("DENTALLY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    # stdio transport speaks JSON-RPC on stdout; a stray log line there corrupts the
    # stream and the client disconnects with an unhelpful parse error.
    stream=sys.stderr,
)
log = logging.getLogger("dentally_mcp")

if config.SENTRY_DSN:  # pragma: no cover
    import sentry_sdk

    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        environment=config.SENTRY_ENVIRONMENT,
        send_default_pii=False,  # never ship patient data to an error tracker
    )


class RoleScopedMCP(FastMCP):
    """Advertise and accept only the tools the calling role may use.

    `tools/list` is billed on every turn: each advertised schema sits in the model's
    context whether or not it is called. Filtering it here is where the token saving
    is. `call_tool` re-checks the same predicate — otherwise the filter would be
    advisory, since a client that cached a tool name could still invoke it.
    """

    async def list_tools(self):
        all_tools = await super().list_tools()
        try:
            allowed = surfaces.visible(auth.current_surface.get())
            return [t for t in all_tools if t.name in allowed]
        except Exception:  # noqa: BLE001
            # Never let the optimisation break discovery. Costing tokens is
            # recoverable; an empty tool list looks like a broken server.
            return all_tools


def build() -> RoleScopedMCP:
    mcp = RoleScopedMCP(
        "dentally",
        instructions=(
            "Tools for a Dentally dental practice: the diary, patients, recalls and "
            "billing. Call `whoami` first if you are unsure which practice you are "
            "connected to or what you may do.\n\n"
            "Rules that matter here:\n"
            "- Always use `find_appointment_slots` before booking. A gap in a diary "
            "listing is not an available slot.\n"
            "- Clinical detail (charting, tooth/surface findings, medical history, "
            "clinical notes) is not available through these tools by design. If asked "
            "for it, say so — do not infer it from other fields.\n"
            "- Patient records are confidential. Return only what was asked for, and "
            "do not repeat patient identifiers into unrelated context."
        ),
        host=config.HOST,
        port=config.PORT,
    )
    tools.register_all(mcp)
    return mcp


def _context_middleware(app):
    """Pure ASGI middleware: turn request headers into per-request context.

    Deliberately not Starlette's BaseHTTPMiddleware — that runs the downstream app in
    a separate task, and context variables set here would not reach the tool call.
    """

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        path = scope.get("path", "")

        # The MCP transport itself is gated; /auth/login and /healthz are not, because
        # a practice has to be able to reach the login page before it has a token.
        if path.startswith("/mcp") and not auth.verify_client(headers.get("authorization")):
            await _send_401(send)
            return

        tok_practice = auth.current_practice.set(headers.get("x-dentally-practice"))
        tok_surface = auth.current_surface.set(
            surfaces.normalise(headers.get("x-dentally-role") or config.DEFAULT_SURFACE))
        tok_caller = auth.current_caller.set(headers.get("x-client-name") or "http")
        try:
            await app(scope, receive, send)
        finally:
            auth.current_practice.reset(tok_practice)
            auth.current_surface.reset(tok_surface)
            auth.current_caller.reset(tok_caller)

    return middleware


async def _send_401(send):
    body = b'{"error":"unauthorized: present the server bearer token"}'
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"www-authenticate", b'Bearer realm="dentally-mcp"'),
    ]})
    await send({"type": "http.response.body", "body": body})


def _wrap_lifespan(app, task_factory) -> None:
    """Run `task_factory()` for the lifetime of the app, alongside its own lifespan."""
    import contextlib

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        task = asyncio.create_task(task_factory())
        try:
            async with inner(scope_app):
                yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.router.lifespan_context = lifespan


def _check_http_safety() -> None:
    """Refuse to expose an unauthenticated server on a non-loopback interface.

    "Convenient locally" turning silently into "open on the internet" is exactly how
    a patient database ends up publicly readable, so this is a hard stop, not a warning.
    """
    loopback = config.HOST in ("127.0.0.1", "localhost", "::1")
    if not config.CLIENT_AUTH_TOKEN and not loopback:
        raise SystemExit(
            f"Refusing to start: bound to {config.HOST} with no DENTALLY_MCP_AUTH_TOKEN set. "
            "Anyone who can reach this port would have full access to the connected "
            "practice's patient records. Set DENTALLY_MCP_AUTH_TOKEN, or bind to 127.0.0.1."
        )


def run_http() -> None:
    import uvicorn

    _check_http_safety()
    mcp = build()
    app = mcp.streamable_http_app()
    for route in http_api.routes():
        app.router.routes.append(route)

    if config.KEEPALIVE_ENABLED and config.TOKEN_STORE_KEY:
        # Dentally tokens die of idleness and have no refresh flow, so this loop is
        # what keeps a hosted integration connected between busy periods.
        #
        # It has to WRAP the existing lifespan rather than use `router.on_startup`:
        # FastMCP installs its own lifespan for the session manager, and Starlette
        # ignores on_startup handlers entirely once a lifespan is set — the keepalive
        # would look wired up and silently never run.
        _wrap_lifespan(app, FLOW.keepalive_loop)

    log.info("Dentally MCP on http://%s:%s/mcp (%s, %s)", config.HOST, config.PORT,
             config.REGION, "writes ON" if config.ALLOW_WRITES else "read-only")
    uvicorn.run(_context_middleware(app), host=config.HOST, port=config.PORT, log_level="info")


def run_stdio() -> None:
    mcp = build()
    auth.current_surface.set(surfaces.normalise(config.DEFAULT_SURFACE))
    auth.current_caller.set("stdio")
    log.info("Dentally MCP on stdio (%s, role=%s, %s)", config.REGION,
             surfaces.normalise(config.DEFAULT_SURFACE),
             "writes ON" if config.ALLOW_WRITES else "read-only")
    mcp.run(transport="stdio")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    transport = os.environ.get("DENTALLY_MCP_TRANSPORT", "stdio").strip().lower()
    if "--http" in argv:
        transport = "http"
    if "--stdio" in argv:
        transport = "stdio"

    if transport in ("http", "streamable-http", "sse"):
        run_http()
    else:
        run_stdio()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
