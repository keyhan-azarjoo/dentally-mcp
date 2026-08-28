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
