"""Role-scoped tool surfaces.

Every tool schema advertised in `tools/list` sits in the model's context on every
single turn, whether or not it is ever called. Showing a receptionist the NHS claims
and revenue tools costs tokens on every message and widens what a prompt injection
can reach, for no benefit.

So the caller picks a surface with the `X-Dentally-Role` header (or the
`DENTALLY_DEFAULT_SURFACE` env var) and sees only that role's tools. `call_tool`
re-checks the same predicate: without that, filtering would be advisory, since a
client that cached a tool name from another session could still invoke it.
"""
from __future__ import annotations

SURFACES: dict[str, set[str]] = {
    # Front desk: the diary and the person in front of them.
    "reception": {
        "whoami", "search_patients", "get_patient", "get_appointment",
        "list_appointments", "find_appointment_slots", "list_practitioners",
        "list_sites", "list_appointment_reasons", "list_cancellation_reasons",
        "book_appointment", "reschedule_appointment", "cancel_appointment",
        "register_patient", "update_patient",
    },
    # Chairside: who is coming, what was planned, what it costs.
    "clinician": {
        "whoami", "search_patients", "get_patient", "get_patient_care_summary",
        "list_appointments", "get_appointment", "list_treatment_plans",
        "list_practitioners", "list_sites", "get_fees",
    },
    # Nursing/hygiene: today's list and recalls, no financial surface at all.
    "nurse": {
        "whoami", "search_patients", "get_patient", "list_appointments",
        "get_appointment", "list_practitioners", "list_sites", "list_recalls_due",
    },
    # Practice management: money, NHS position, utilisation.
    "manager": {
        "whoami", "search_patients", "get_patient", "list_appointments",
        "list_practitioners", "list_sites", "list_invoices", "list_payments",
        "get_patient_account", "list_nhs_claims", "revenue_summary",
        "list_recalls_due", "diary_utilisation", "get_fees",
    },
}

# Everything, for a trusted operator or an integration test.
SURFACES["full"] = set().union(*SURFACES.values())

DEFAULT = "reception"

# Tools that change state in Dentally. Kept as one list so the write gate, the
# consent prompt and the audit classifier can never drift apart.
WRITE_TOOLS: set[str] = {
    "book_appointment", "reschedule_appointment", "cancel_appointment",
    "register_patient", "update_patient",
}


def normalise(role: str | None) -> str:
    if not role:
        return DEFAULT
    role = role.strip().lower()
    aliases = {
        "receptionist": "reception", "front-desk": "reception", "frontdesk": "reception",
        "dentist": "clinician", "hygienist": "nurse", "therapist": "nurse",
        "practice-manager": "manager", "admin": "manager", "owner": "manager",
        "all": "full", "*": "full",
    }
    role = aliases.get(role, role)
    return role if role in SURFACES else DEFAULT


def visible(role: str | None) -> set[str]:
    return SURFACES[normalise(role)]


def allows(role: str | None, tool: str) -> bool:
    return tool in visible(role)


def is_write(tool: str) -> bool:
    return tool in WRITE_TOOLS


# --- Tool annotations --------------------------------------------------------
# Every tool declares all four MCP hints, explicitly, as booleans.
#
# These are what let a host warn a user before a call — "this will change your
# diary" — and a missing hint is not neutral: OpenAI's directory rejects a tool
# where any of the four is absent or non-boolean, and a host that cannot tell a read
# from a write has to either warn on everything or warn on nothing.
#
# The values describe what the handler ACTUALLY does:
#   readOnlyHint    - does not modify the practice's data
#   destructiveHint - overwrites or removes existing state (vs. only adding)
#   idempotentHint  - calling twice with the same arguments leaves the same end state
#   openWorldHint   - true for all of these: every one calls the Dentally API
#
# `_WRITE_ANNOTATIONS` is checked against `WRITE_TOOLS` by a test, so the hint a host
# shows the user can never drift from the gate that actually enforces it.
_WRITE_ANNOTATIONS: dict[str, tuple[bool, bool]] = {
    # name: (destructiveHint, idempotentHint)
    #
    # Creates add a new record: not destructive, and NOT idempotent — calling twice
    # books two appointments or registers the same patient twice.
    "book_appointment": (False, False),
    "register_patient": (False, False),
    # Updates overwrite existing state, so they are destructive; but the end state
    # depends only on the arguments, so repeating one changes nothing further.
    "reschedule_appointment": (True, True),
    "cancel_appointment": (True, True),
    "update_patient": (True, True),
}


def hints(tool: str):
    """The four MCP annotations for a tool, all explicit booleans."""
    from mcp.types import ToolAnnotations

    if tool in WRITE_TOOLS:
        destructive, idempotent = _WRITE_ANNOTATIONS[tool]
        return ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=True,
        )
    return ToolAnnotations(
        readOnlyHint=True,
        # Meaningless when readOnlyHint is true, but stated anyway: a host that reads
        # them independently must not see a missing field and guess.
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
