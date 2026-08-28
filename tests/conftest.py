import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Pin the environment BEFORE dentally_mcp.config is imported: it reads env at import
# time, so a stray real token in the developer's shell would otherwise leak into the
# tests and, worse, let them hit production Dentally.
os.environ.setdefault("DENTALLY_REGION", "sandbox")
os.environ.pop("DENTALLY_API_TOKEN", None)
os.environ.pop("DENTALLY_CLIENT_ID", None)
os.environ.pop("DENTALLY_CLIENT_SECRET", None)
os.environ.setdefault("DENTALLY_ALLOW_WRITES", "0")
os.environ.setdefault("DENTALLY_AUDIT_LOG", "")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_context():
    """Tools read per-request context vars; leaking one between tests hides bugs."""
    from dentally_mcp import auth

    p = auth.current_practice.set(None)
    s = auth.current_surface.set("full")
    c = auth.current_caller.set("test")
    yield
    auth.current_practice.reset(p)
    auth.current_surface.reset(s)
    auth.current_caller.reset(c)
