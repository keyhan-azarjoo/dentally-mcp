"""Patient lookup and registration."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from .. import redaction
from ..client import clamp_date_window, safe_id
from ..errors import DentallyError, ValidationError
from ..surfaces import hints
from ..runtime import confirm, tool, use_client


def register(mcp) -> None:

    @mcp.tool(annotations=hints("search_patients"))
    @tool("search_patients")
    async def search_patients(
        query: str,
        limit: int = 10,
        site_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Find patients by name, or look one up by their Dentally patient ID.

        Returns a short summary per patient — never clinical notes, charting, or
        medical history. Use `get_patient` for one patient's detail.

        Args:
            query: A name (partial is fine) or a numeric Dentally patient ID.
            limit: Maximum patients to return (1-50). Keep it small.
            site_id: Restrict to one site in a multi-site practice.
            include_inactive: Include patients marked inactive.
        """
        query = (query or "").strip()
        if not query:
            raise ValidationError("search_patients needs a name or patient ID.")
        limit = max(1, min(int(limit), 50))

        async with use_client() as client:
            # A bare number is almost always someone pasting a patient ID; going
            # straight to the record avoids a fuzzy search that may not surface it.
            if query.isdigit():
                try:
                    body = await client.get(f"patients/{safe_id(query, 'patient_id')}")
                    patient = body.get("patient") if isinstance(body, dict) else None
                    if patient:
                        return [redaction.scrub(redaction.patient_summary(patient))]
                except DentallyError:
                    pass  # fall through to the name search

            params: dict[str, Any] = {"search": query}
            if site_id:
                params["site_id"] = site_id
            if not include_inactive:
                params["active"] = True

            records = await client.list_all("patients", "patients", limit=limit, **params)
            return redaction.project(records, redaction.patient_summary)

    @mcp.tool(annotations=hints("get_patient"))
    @tool("get_patient")
    async def get_patient(patient_id: str) -> dict[str, Any]:
        """Get one patient's demographic and administrative record.

        Clinical detail (charting, tooth/surface findings, medical history, notes) is
        deliberately not available through this server.

        Args:
            patient_id: The Dentally patient ID.
        """
        async with use_client() as client:
            body = await client.get(f"patients/{safe_id(patient_id, 'patient_id')}")
        patient = body.get("patient") if isinstance(body, dict) else body
        if not patient:
            raise DentallyError(f"No patient {patient_id} in Dentally.")
        return redaction.scrub(redaction.patient_summary(patient))

    @mcp.tool(annotations=hints("get_patient_care_summary"))
    @tool("get_patient_care_summary")
    async def get_patient_care_summary(patient_id: str) -> dict[str, Any]:
        """A patient's upcoming appointments, treatment plans and account balance.

        The single call to make before seeing a patient: who they are, when they are
        next in, what is planned, and whether they owe anything.

        Args:
            patient_id: The Dentally patient ID.
        """
        async with use_client() as client:
            body = await client.get(f"patients/{safe_id(patient_id, 'patient_id')}")
            patient = body.get("patient") if isinstance(body, dict) else {}

            appts = await client.list_all(
                "appointments", "appointments", limit=10,
                patient_id=patient_id, sort_by="start_time", sort_direction="desc",
            )
            plans = await _safe_list(client, "treatment_plans", "treatment_plans", limit=10, patient_id=patient_id)
            accounts = await _safe_list(client, "accounts", "accounts", limit=5, patient_id=patient_id)

        return redaction.scrub({
            "patient": redaction.patient_summary(patient or {}),
            "appointments": [redaction.appointment_summary(a) for a in appts],
            "treatment_plans": [_plan_summary(p) for p in plans],
            "accounts": [redaction.money_summary(a) for a in accounts],
        })

    @mcp.tool(annotations=hints("list_treatment_plans"))
    @tool("list_treatment_plans")
    async def list_treatment_plans(patient_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """List a patient's treatment plans: status, total, and how much is outstanding.

        Item-level tooth and surface detail is stripped — this answers "what is planned
        and what does it cost", not "what is the clinical finding".

        Args:
            patient_id: The Dentally patient ID.
            limit: Maximum plans to return.
        """
        async with use_client() as client:
            plans = await _safe_list(client, "treatment_plans", "treatment_plans",
                                     limit=max(1, min(int(limit), 50)), patient_id=patient_id)
        return [redaction.scrub(_plan_summary(p)) for p in plans]

    @mcp.tool(annotations=hints("register_patient"))
    @tool("register_patient")
    async def register_patient(
        first_name: str,
        last_name: str,
        date_of_birth: str,
        site_id: str | None = None,
        email: str | None = None,
        mobile_phone: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new patient record in Dentally. Requires confirmation.

        Args:
            first_name: Patient's first name.
            last_name: Patient's last name.
            date_of_birth: YYYY-MM-DD.
            site_id: Site to register them at (required in multi-site practices).
            email: Contact email, if given.
            mobile_phone: Contact mobile, if given.
        """
        clamp_date_window(date_of_birth, date_of_birth, max_days=0)  # validates the format
        await confirm(
            ctx,
            "Create a new patient in Dentally",
            f"{first_name} {last_name}, born {date_of_birth}"
            + (f", site {site_id}" if site_id else ""),
        )
        payload = {"patient": _drop_empty({
            "first_name": first_name,
            "last_name": last_name,
            "date_of_birth": date_of_birth,
            "site_id": site_id,
            "email_address": email,
            "mobile_phone": mobile_phone,
        })}
        async with use_client() as client:
            body = await client.post("patients", payload)
        created = body.get("patient") if isinstance(body, dict) else body
        return redaction.scrub(redaction.patient_summary(created or {}))

    @mcp.tool(annotations=hints("update_patient"))
    @tool("update_patient")
    async def update_patient(
        patient_id: str,
        email: str | None = None,
        mobile_phone: str | None = None,
        address_line_1: str | None = None,
        postcode: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a patient's contact details. Requires confirmation.

        Only contact fields are editable here. Clinical and financial fields are out of
        scope for an assistant on purpose.

        Args:
            patient_id: The Dentally patient ID.
            email: New email address.
            mobile_phone: New mobile number.
            address_line_1: New first address line.
            postcode: New postcode.
        """
        changes = _drop_empty({
            "email_address": email,
            "mobile_phone": mobile_phone,
            "address_line_1": address_line_1,
            "postcode": postcode,
        })
        if not changes:
            raise ValidationError("update_patient needs at least one field to change.")
        await confirm(ctx, "Update patient contact details in Dentally",
                      f"Patient {patient_id}: change {', '.join(sorted(changes))}")
        async with use_client() as client:
            body = await client.put(f"patients/{safe_id(patient_id, 'patient_id')}", {"patient": changes})
        updated = body.get("patient") if isinstance(body, dict) else body
        return redaction.scrub(redaction.patient_summary(updated or {}))


async def _safe_list(client, path: str, key: str, **kw) -> list[dict]:
    """List a resource, treating 'not available on this plan/scope' as empty.

    Several Dentally collections are gated by scope or by whether the practice uses
    that module. A composite summary should degrade to a missing section, not fail
    the whole call.
    """
    try:
        return await client.list_all(path, key, **kw)
    except DentallyError:
        return []


def _plan_summary(p: dict) -> dict:
    out = {
        "id": p.get("id"),
        "name": p.get("name") or p.get("title"),
        "state": p.get("state") or p.get("status"),
        "total": p.get("total") or p.get("gross_price"),
        "outstanding": p.get("outstanding"),
        "created_at": p.get("created_at"),
        "practitioner_id": p.get("practitioner_id"),
        # Count only. Item detail is tooth/surface level and stays inside Dentally.
        "item_count": len(p.get("treatment_plan_items") or []) or p.get("items_count"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "")}
