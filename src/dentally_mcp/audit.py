"""Append-only audit trail of every tool call.

A practice that lets an AI touch patient records needs to be able to answer "who
looked at that patient, when, and through what" — for its own governance and for a
UK GDPR subject access or breach investigation. The model cannot be the witness, so
the server records it.

What is deliberately NOT recorded: patient names, the tool's return value, and any
argument that looks like a credential. An audit log that duplicates the records it
is auditing is just a second copy of the patient database with weaker access control.

Free-text arguments are stored as a short digest rather than dropped, so the log can
still answer "the same search ran twice" and "how many distinct patients did this
session touch" without holding the names themselves.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

from . import config

log = logging.getLogger("dentally_mcp.audit")

_SENSITIVE_ARG_MARKERS = ("token", "secret", "password", "authorization", "api_key")

# Arguments safe to record verbatim: identifiers, dates, and filters — which is what
# an investigation actually needs to reconstruct a session.
#
# This is an ALLOW-list, not a deny-list, and that direction is the point. The old
# deny-list recorded everything it did not recognise, so `search_patients(query="Jane
# Doe")` wrote a patient's name into the audit file — while this module's own
# docstring promised patient names were never recorded. A deny-list also fails open
# for every argument added in future. Anything not listed here is hashed instead.
_VERBATIM_ARGS = frozenset({
    "limit", "page", "per_page", "start_date", "end_date", "start_time", "finish_time",
    "duration_minutes", "state", "sort_by", "sort_direction", "active_only",
    "include_inactive", "label", "role", "practice",
})
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
        low = k.lower()

        if any(marker in low for marker in _SENSITIVE_ARG_MARKERS):
            out[k] = "[redacted]"
            continue

        # Nothing identifying can hide in a bool, a number, or an absent value.
        if value is None or isinstance(value, (bool, int, float)):
            out[k] = value
            continue

        if low.endswith("_id") or low == "id" or low in _VERBATIM_ARGS:
            text = str(value)
            out[k] = text if len(text) <= _MAX_ARG_LEN else text[:_MAX_ARG_LEN] + "…"
            continue

        # Everything else — search terms, names, notes, contact details — is stored
        # as a short digest. That still answers "was this the same search, twice?"
        # and "how many distinct patients did this session touch?", which is what an
        # investigation needs, without the audit log becoming a second copy of the
        # patient database under weaker access control.
        out[k] = _digest(value)
    return out


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
