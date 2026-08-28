#!/usr/bin/env python3
"""Connect a practice from the command line, and prove the credential works.

    python scripts/login.py --check              # validate DENTALLY_API_TOKEN
    python scripts/login.py --token <token>      # validate a token you were given
    python scripts/login.py --store <token>      # validate AND save it
    python scripts/login.py --url                # print the OAuth authorize URL
    python scripts/login.py --list               # show connected practices
    python scripts/login.py --keepalive          # ping every stored token now

Worth running before wiring up an AI client: a credential problem fails here with a
message that says what to do, instead of surfacing three tool calls later as an
unexplained 403.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dentally_mcp import config  # noqa: E402
from dentally_mcp.errors import DentallyError  # noqa: E402
from dentally_mcp.oauth import OAuthFlow  # noqa: E402
from dentally_mcp.tokenstore import TokenStore  # noqa: E402


async def run(args) -> int:
    flow = OAuthFlow(TokenStore())

    print(f"Region      : {config.REGION}")
    print(f"API base    : {config.API_BASE}")
    print(f"Environment : {'SANDBOX' if config.IS_SANDBOX else 'PRODUCTION'}")
    print()

    if args.url:
        try:
            url, state = flow.authorize_url(practice_hint=args.practice)
        except DentallyError as exc:
            print(f"✗ {exc.message}")
            return 1
        print("Open this in a browser to connect a practice:\n")
        print(url)
        print(f"\n(state: {state} — valid for 10 minutes)")
        return 0

    if args.list:
        if not config.TOKEN_STORE_KEY:
            print("No DENTALLY_TOKEN_KEY set, so no practices are stored.")
            print("Generate one with: python -m dentally_mcp.tokenstore --genkey")
            return 1
        practices = TokenStore().all()
        if not practices:
            print("No practices connected yet.")
            return 0
        for tok in practices:
            print(json.dumps(tok.redacted(), indent=2))
        return 0

    if args.keepalive:
        results = await flow.keepalive_once()
        if not results:
            print("Nothing stored to keep alive.")
            return 0
        for practice, status in results.items():
            print(f"{'✓' if status == 'ok' else '✗'} {practice}: {status}")
        return 0 if all(v == "ok" for v in results.values()) else 1

    token = args.token or args.store or config.API_TOKEN
    if not token:
        print("✗ No token. Set DENTALLY_API_TOKEN, or pass --token <token>.")
        print("  See docs/LOGIN.md for how to generate one in Dentally.")
        return 1

    try:
        identity = await flow.identify(token)
    except DentallyError as exc:
        print(f"✗ Dentally rejected the token: {exc.message}")
        return 1

    print("✓ Token is valid.\n")
    print(f"  Practice  : {identity.get('practice_name') or identity['practice_id']}")
    print(f"  Practice ID: {identity['practice_id']}")
    print(f"  User      : {identity.get('user_name') or '(not reported)'}")
    scopes = identity.get("scopes")
    print(f"  Scopes    : {' '.join(scopes) if scopes else '(not reported by Dentally)'}")

    if args.store:
        if not config.TOKEN_STORE_KEY:
            print("\n✗ Cannot store it: DENTALLY_TOKEN_KEY is not set.")
            print("  Generate one with: python -m dentally_mcp.tokenstore --genkey")
            return 1
        stored = await flow.register_api_token(args.store, label=args.label)
        print(f"\n✓ Stored. Use header: X-Dentally-Practice: {stored.practice_id}")

    if not config.IS_SANDBOX:
        print("\n⚠  This is PRODUCTION — real patient records. Read docs/SECURITY.md.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Dentally MCP login helper")
    p.add_argument("--check", action="store_true", help="validate DENTALLY_API_TOKEN")
    p.add_argument("--token", help="validate this token (does not store it)")
    p.add_argument("--store", help="validate and store this token")
    p.add_argument("--label", help="a name for the stored practice")
    p.add_argument("--practice", help="practice hint for the OAuth flow")
    p.add_argument("--url", action="store_true", help="print the OAuth authorize URL")
    p.add_argument("--list", action="store_true", help="list connected practices")
    p.add_argument("--keepalive", action="store_true", help="ping every stored token")
    args = p.parse_args()

    if not any([args.check, args.token, args.store, args.url, args.list, args.keepalive]):
        p.print_help()
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
