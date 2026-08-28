"""Append-only audit trail of every tool call.

A practice that lets an AI touch patient records needs to be able to answer "who
looked at that patient, when, and through what" — for its own governance and for a
UK GDPR subject access or breach investigation. The model cannot be the witness, so
the server records it.

What is deliberately NOT recorded: patient names, the tool's return value, and any
argument that looks like a credential. An audit log that duplicates the records it
is auditing is just a second copy of the patient database with weaker access control.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

from . import config

log = logging.getLogger("dentally_mcp.audit")

_SENSITIVE_ARG_MARKERS = ("token", "secret", "password", "authorization", "api_key")
# Arguments worth keeping: identifiers and filters, which is what an investigation
# actually needs to reconstruct a session.
_MAX_ARG_LEN = 120


def record(
    *,
    tool: str,
    practice_id: str | None,
    caller: str,
    args: Mapping[str, Any] | None = None,
    outcome: str = "ok",
    error: str | None = None,
    count: int | None = None,
) -> dict:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "practice_id": practice_id,
        "caller": caller,
        "args": _safe_args(args or {}),
        "outcome": outcome,
        "result_count": count,
    }
    if error:
        entry["error"] = error[:300]

    if config.AUDIT_LOG:
        try:
            path = Path(config.AUDIT_LOG)
            path.parent.mkdir(parents=True, exist_ok=True)
            newly_created = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            if newly_created:
                # The log names patients by id and practices by name; it is not public.
                os.chmod(path, 0o600)
        except OSError as exc:
            # A failing audit sink must not take the tool call down with it, but it
            # must be loud — silently losing the trail is the worst of both worlds.
            log.error("Audit write failed (%s): %s", config.AUDIT_LOG, exc)
    else:
        log.info("audit %s", json.dumps(entry, separators=(",", ":")))
    return entry


def _safe_args(args: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        k = str(key)
        if any(marker in k.lower() for marker in _SENSITIVE_ARG_MARKERS):
            out[k] = "[redacted]"
            continue
        text = str(value)
        out[k] = text if len(text) <= _MAX_ARG_LEN else text[:_MAX_ARG_LEN] + "…"
    return out
