"""`python -m dentally_mcp` — stdio by default, `--http` for the hosted transport."""
from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
