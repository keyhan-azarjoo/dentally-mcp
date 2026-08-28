# dentally-mcp — notes for anyone (human or agent) changing this code

This server sits between a language model and a dental practice's patient records.
Most of the code is ordinary; a few things are load-bearing in ways that are not
obvious from reading the diff.

## Do not weaken these without a deliberate decision

1. **Clinical detail is stripped unconditionally** (`redaction.py`). There is no flag
   to disable it. Filtering is by field-name pattern so a new Dentally field is
   excluded by default. Adding an allow-list here would invert that.

2. **Writes are off by default and consent fails closed** (`config.ALLOW_WRITES`,
   `runtime.confirm`). A client that cannot ask a human does not get to write. The
   attack this defends against is a prompt injected through a patient record, so
   "the model checked" is not a substitute.

3. **`call_tool` re-checks the role**, not just `list_tools` (`server.py`,
   `runtime.tool`). Filtering the listing alone is advisory — a client can cache a
   tool name from another session.

4. **The caller never supplies a Dentally token** (`auth.Resolver`). It names a
   practice; the server resolves the credential. Otherwise one leaked token reaches
   every practice that ever authorised us.

5. **The server refuses a non-loopback bind without `DENTALLY_MCP_AUTH_TOKEN`**
   (`server._check_http_safety`). Hard failure, not a warning.

6. **Pagination and date ranges stay bounded** (`client.list_all`,
   `clamp_date_window`). Dentally's rate budget is per-user and shared with the
   practice's online booking; an unbounded loop takes their booking page down.

## Two traps that look like nothing

**`runtime.tool` resolves `__annotations__` deliberately.** The tool modules use
`from __future__ import annotations`, and FastMCP resolves a tool's hints against the
*wrapper's* globals — where `Context` is not defined. Remove that fix-up and FastMCP
decides the tools want no Context, `ctx` arrives as None, and every write silently
loses its confirmation prompt while still looking wired up. `test_tools.py` guards it.

**The keepalive wraps the lifespan, it does not use `on_startup`.** FastMCP installs
its own lifespan, and Starlette ignores `on_startup` handlers once one is set. Wiring
it the obvious way produces a keepalive that looks configured and never runs — which,
given Dentally has no token refresh, means every practice silently disconnects.

## Verified vs inferred API surface

`docs/DENTALLY_API.md` marks each resource ✅ or ⚠️. The ⚠️ ones were inferred from
third-party integrations because Dentally's docs page truncates when fetched. Tools
built on them degrade to an empty section on 404 rather than failing the call. When
you get production access, confirm them and update the markers — do not quietly
promote one to ✅ without checking.

## Conventions

* Every tool goes through `@tool("name")` for the surface check, write gate, audit
  and error translation. A tool that skips it has no guard rails.
* Every tool returns a hand-picked projection, never a raw Dentally object.
* Tests mock Dentally. No test may need a real credential; CI asserts that.
* `pytest -q && python scripts/smoke.py` before pushing.
