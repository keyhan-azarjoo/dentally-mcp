"""Encrypted at-rest storage for per-practice Dentally tokens.

A Dentally token is a bearer credential for an entire dental practice's patient
records. Storing it as plaintext JSON next to the code would mean a single stray
`cat` or a backup snapshot leaks every patient in that practice, so the file is
Fernet-encrypted and written 0600.

The key comes from the environment and is never persisted here. Losing it means
re-authorising the practices, which is the correct failure mode.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .errors import DentallyError

try:  # pragma: no cover - exercised by whichever branch the environment has
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    _HAVE_CRYPTO = False


@dataclass
class PracticeToken:
    """One practice's credential and the metadata needed to keep it alive."""

    practice_id: str
    access_token: str
    # Dentally issues no refresh token; this is stored only because the OAuth
    # response shape allows it and a future API change may start populating it.
    refresh_token: str | None = None
    scopes: list[str] = field(default_factory=list)
    site_id: str | None = None
    practice_name: str | None = None
    region: str = config.REGION
    created_at: float = field(default_factory=time.time)
    # Last time we successfully used it. Dentally expires tokens on IDLENESS, so this
    # is the number the keepalive job actually acts on.
    last_used_at: float = field(default_factory=time.time)

    def redacted(self) -> dict[str, Any]:
        """Safe to log, return from an API, or show a user."""
        d = asdict(self)
        d["access_token"] = _mask(self.access_token)
        d["refresh_token"] = _mask(self.refresh_token) if self.refresh_token else None
        return d


class TokenStore:
    """Practice-id -> PracticeToken, encrypted on disk."""

    def __init__(self, path: str | None = None, key: str | None = None):
        self.path = Path(path or config.TOKEN_STORE_PATH)
        self._key = (key if key is not None else config.TOKEN_STORE_KEY) or ""
        self._cache: dict[str, PracticeToken] | None = None

    # -- crypto ---------------------------------------------------------------
    def _fernet(self):
        if not self._key:
            raise DentallyError(
                "DENTALLY_TOKEN_KEY is not set, so practice tokens cannot be stored "
                "safely. Generate one with: python -m dentally_mcp.tokenstore --genkey"
            )
        if not _HAVE_CRYPTO:
            raise DentallyError(
                "The 'cryptography' package is required to store practice tokens. "
                "Install it with: pip install cryptography"
            )
        try:
            return Fernet(self._key.encode() if isinstance(self._key, str) else self._key)
        except (ValueError, TypeError) as exc:
            raise DentallyError(f"DENTALLY_TOKEN_KEY is not a valid Fernet key: {exc}") from exc

    # -- persistence ----------------------------------------------------------
    def _load(self) -> dict[str, PracticeToken]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        blob = self.path.read_bytes()
        if not blob.strip():
            self._cache = {}
            return self._cache
        try:
            raw = self._fernet().decrypt(blob)
        except InvalidToken as exc:
            raise DentallyError(
                f"Could not decrypt {self.path} — DENTALLY_TOKEN_KEY does not match the "
                "key the file was written with."
            ) from exc
        data = json.loads(raw.decode())
        self._cache = {k: PracticeToken(**v) for k, v in data.items()}
        return self._cache

    def _flush(self) -> None:
        data = {k: asdict(v) for k, v in (self._cache or {}).items()}
        blob = self._fernet().encrypt(json.dumps(data).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        # Atomic replace: a crash mid-write must never leave a half-file that would
        # lock every practice out.
        tmp.replace(self.path)

    # -- api ------------------------------------------------------------------
    def put(self, token: PracticeToken) -> PracticeToken:
        store = self._load()
        store[token.practice_id] = token
        self._flush()
        return token

    def get(self, practice_id: str) -> PracticeToken | None:
        return self._load().get(practice_id)

    def delete(self, practice_id: str) -> bool:
        store = self._load()
        if practice_id in store:
            del store[practice_id]
            self._flush()
            return True
        return False

    def list_ids(self) -> list[str]:
        return sorted(self._load().keys())

    def all(self) -> list[PracticeToken]:
        return list(self._load().values())

    def touch(self, practice_id: str) -> None:
        """Record a successful use — the keepalive job schedules from this."""
        store = self._load()
        tok = store.get(practice_id)
        if tok:
            tok.last_used_at = time.time()
            self._flush()


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _main() -> int:  # pragma: no cover - operator utility
    import sys

    if "--genkey" in sys.argv:
        if not _HAVE_CRYPTO:
            print("pip install cryptography first", file=sys.stderr)
            return 1
        print(Fernet.generate_key().decode())
        return 0
    if "--list" in sys.argv:
        for tok in TokenStore().all():
            print(json.dumps(tok.redacted()))
        return 0
    print(__doc__)
    print("\nusage: python -m dentally_mcp.tokenstore [--genkey | --list]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
