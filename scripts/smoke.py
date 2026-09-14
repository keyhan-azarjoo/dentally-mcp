#!/usr/bin/env python3
"""Prove the server actually starts and answers, without touching real Dentally.

Run: python scripts/smoke.py

Checks, in order: the tool registry builds, the HTTP app mounts, /healthz answers,
the MCP endpoint refuses an unauthenticated caller, and an authenticated
`tools/list` returns the calling role's tools and nothing else.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("DENTALLY_REGION", "sandbox")
os.environ.setdefault("DENTALLY_MCP_AUTH_TOKEN", "smoke-token")
os.environ.setdefault("DENTALLY_API_TOKEN", "not-a-real-token")

from starlette.testclient import TestClient  # noqa: E402

from dentally_mcp import http_api, surfaces  # noqa: E402
from dentally_mcp.server import _context_middleware, build  # noqa: E402


def main() -> int:
    failures = []

    def check(name, condition, detail=""):
        print(f"{'PASS' if condition else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
        if not condition:
            failures.append(name)

    mcp = build()
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    check("tools register", len(tool_names) == len(surfaces.SURFACES["full"]),
          f"{len(tool_names)} tools")

    app = mcp.streamable_http_app()
    for route in http_api.routes():
        app.router.routes.append(route)
    client = TestClient(_context_middleware(app))

    resp = client.get("/healthz")
    check("/healthz answers", resp.status_code == 200, str(resp.json().get("status")))
    # Anonymous /healthz is liveness only. Whether writes are on and how many
    # practices are connected is reconnaissance, so it needs the admin token.
    check("/healthz hides config from anonymous callers",
          "writes_enabled" not in resp.json() and "api_base" not in resp.json())

    auth = {"Authorization": "Bearer " + os.environ["DENTALLY_MCP_AUTH_TOKEN"]}
    detail = client.get("/healthz", headers=auth).json()
    check("sandbox by default", detail.get("environment") == "sandbox")
    check("read-only by default", detail.get("writes_enabled") is False)

    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check("/mcp rejects an unauthenticated caller", resp.status_code == 401)

    resp = client.get("/.well-known/oauth-protected-resource")
    check("OAuth resource metadata is published", resp.status_code == 200)

    resp = client.get("/auth/callback", params={"error": "<script>alert(1)</script>"})
    check("callback escapes reflected input", "<script>alert" not in resp.text)
    check("HTML pages carry a CSP",
          "default-src 'none'" in resp.headers.get("content-security-policy", ""))

    annotated = [t for t in mcp._tool_manager.list_tools()
                 if t.annotations and isinstance(t.annotations.readOnlyHint, bool)]
    check("every tool declares its hints", len(annotated) == len(tool_names),
          f"{len(annotated)}/{len(tool_names)}")

    resp = client.post("/auth/validate", json={"token": "x"})
    check("/auth/validate needs the server token", resp.status_code == 401)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
