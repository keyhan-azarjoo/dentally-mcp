"""The diary: what is booked, what is free, and changing it."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from .. import redaction
from ..client import clamp_date_window
from ..errors import DentallyError, ValidationError
from ..runtime import confirm, tool, use_client


def register(mcp) -> None:

    @mcp.tool()
    @tool("list_appointments")
    async def list_appointments(
        start_date: str | None = None,
        end_date: str | None = None,
        practitioner_id: str | None = None,
        site_id: str | None = None,
        patient_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List appointments in a date range — the practice diary.

        With no dates this returns today. The range is capped at about three months
        because Dentally's own guidance is that wider filters on this endpoint time out.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            practitioner_id: Restrict to one dentist/hygienist.
            site_id: Restrict to one site.
            patient_id: Restrict to one patient.
            state: Appointment state, e.g. 'active', 'completed', 'cancelled'.
            limit: Maximum appointments to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            records = await client.list_all(
                "appointments", "appointments",
                limit=max(1, min(int(limit), 200)),
                start_time=f"{start}T00:00:00",
                finish_time=f"{end}T23:59:59",
                practitioner_id=practitioner_id,
                site_id=site_id,
                patient_id=patient_id,
                state=state,
                sort_by="start_time",
                sort_direction="asc",
            )
        return redaction.project(records, redaction.appointment_summary)

    @mcp.tool()
    @tool("get_appointment")
    async def get_appointment(appointment_id: str) -> dict[str, Any]:
        """Get one appointment: time, duration, state, patient and practitioner.

        Args:
            appointment_id: The Dentally appointment ID.
        """
        async with use_client() as client:
            body = await client.get(f"appointments/{appointment_id}")
        appt = body.get("appointment") if isinstance(body, dict) else body
        if not appt:
            raise DentallyError(f"No appointment {appointment_id} in Dentally.")
        return redaction.scrub(redaction.appointment_summary(appt))

    @mcp.tool()
    @tool("find_appointment_slots")
    async def find_appointment_slots(
        start_date: str,
        end_date: str | None = None,
        duration_minutes: int = 30,
        practitioner_id: str | None = None,
        site_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find free appointment slots — what you can actually offer a patient.

        Always call this before booking. Booking a time taken from a diary listing
        rather than from availability is how you double-book a surgery.

        Args:
            start_date: First day to search, YYYY-MM-DD.
            end_date: Last day to search. Defaults to start_date.
            duration_minutes: Length of appointment needed.
            practitioner_id: Restrict to one practitioner.
            site_id: Restrict to one site.
            limit: Maximum slots to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            body = await client.get(
                "appointments/availability",
                start_time=f"{start}T00:00:00",
                finish_time=f"{end}T23:59:59",
                duration=int(duration_minutes),
                practitioner_id=practitioner_id,
                site_id=site_id,
            )
        slots = []
        if isinstance(body, dict):
            slots = body.get("availability") or body.get("slots") or body.get("appointments") or []
        elif isinstance(body, list):
            slots = body
        out = [
            {k: v for k, v in {
                "start_time": s.get("start_time") or s.get("from"),
                "finish_time": s.get("finish_time") or s.get("to"),
                "practitioner_id": s.get("practitioner_id"),
                "site_id": s.get("site_id"),
                "duration_minutes": s.get("duration") or duration_minutes,
            }.items() if v is not None}
            for s in slots if isinstance(s, dict)
        ]
        return out[: max(1, min(int(limit), 100))]

    @mcp.tool()
    @tool("book_appointment")
    async def book_appointment(
        patient_id: str,
        practitioner_id: str,
        start_time: str,
        duration_minutes: int = 30,
        reason_id: str | None = None,
        site_id: str | None = None,
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Book an appointment. Requires confirmation.

        Take `start_time` from `find_appointment_slots`, not from a gap you inferred
        in a diary listing — a listing does not show blocks, leave, or surgery closures.

        Args:
            patient_id: The Dentally patient ID.
            practitioner_id: The practitioner to book with.
            start_time: ISO 8601 start, e.g. 2026-09-14T09:30:00.
            duration_minutes: Length of the appointment.
            reason_id: Appointment reason ID from `list_appointment_reasons`.
            site_id: Site to book at.
            notes: Short administrative note. Do not put clinical detail here.
        """
        _require_iso(start_time, "start_time")
        await confirm(
            ctx,
            "Book an appointment in Dentally",
            f"Patient {patient_id} with practitioner {practitioner_id} "
            f"at {start_time} for {duration_minutes} minutes.",
        )
        payload = {"appointment": _drop_empty({
            "patient_id": patient_id,
            "practitioner_id": practitioner_id,
            "start_time": start_time,
            "duration": int(duration_minutes),
            "reason_id": reason_id,
            "site_id": site_id,
            "notes": notes,
        })}
        async with use_client() as client:
            body = await client.post("appointments", payload)
        appt = body.get("appointment") if isinstance(body, dict) else body
        return redaction.scrub(redaction.appointment_summary(appt or {}))

    @mcp.tool()
    @tool("reschedule_appointment")
    async def reschedule_appointment(
        appointment_id: str,
        start_time: str,
        duration_minutes: int | None = None,
        practitioner_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move an existing appointment to a new time. Requires confirmation.

        Args:
            appointment_id: The appointment to move.
            start_time: New ISO 8601 start time.
            duration_minutes: New duration, if it changes.
            practitioner_id: New practitioner, if it changes.
        """
        _require_iso(start_time, "start_time")
        await confirm(ctx, "Move an appointment in Dentally",
                      f"Appointment {appointment_id} moves to {start_time}.")
        changes = _drop_empty({
            "start_time": start_time,
            "duration": duration_minutes,
            "practitioner_id": practitioner_id,
        })
        async with use_client() as client:
            body = await client.put(f"appointments/{appointment_id}", {"appointment": changes})
        appt = body.get("appointment") if isinstance(body, dict) else body
        return redaction.scrub(redaction.appointment_summary(appt or {}))

    @mcp.tool()
    @tool("cancel_appointment")
    async def cancel_appointment(
        appointment_id: str,
        cancellation_reason_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Cancel an appointment. Requires confirmation.

        This sets the appointment's state to cancelled rather than deleting it, so the
        practice keeps its cancellation and failed-to-attend reporting.

        Args:
            appointment_id: The appointment to cancel.
            cancellation_reason_id: Reason ID from `list_cancellation_reasons`.
        """
        await confirm(ctx, "Cancel an appointment in Dentally",
                      f"Appointment {appointment_id} will be cancelled.")
        changes = _drop_empty({
            "state": "cancelled",
            "cancellation_reason_id": cancellation_reason_id,
        })
        async with use_client() as client:
            body = await client.put(f"appointments/{appointment_id}", {"appointment": changes})
        appt = body.get("appointment") if isinstance(body, dict) else body
        return redaction.scrub(redaction.appointment_summary(appt or {}))

    @mcp.tool()
    @tool("list_appointment_reasons")
    async def list_appointment_reasons() -> list[dict[str, Any]]:
        """List the practice's appointment reasons and their default durations."""
        async with use_client() as client:
            records = await client.list_all("appointment_reasons", "appointment_reasons", limit=100)
        return [
            {k: v for k, v in {
                "id": r.get("id"),
                "name": r.get("name"),
                "duration_minutes": r.get("duration"),
                "active": r.get("active"),
            }.items() if v is not None}
            for r in records
        ]

    @mcp.tool()
    @tool("list_cancellation_reasons")
    async def list_cancellation_reasons() -> list[dict[str, Any]]:
        """List the practice's appointment cancellation reasons."""
        async with use_client() as client:
            records = await client.list_all(
                "appointment_cancellation_reasons", "appointment_cancellation_reasons", limit=100)
        return [{"id": r.get("id"), "name": r.get("name")} for r in records]

    @mcp.tool()
    @tool("diary_utilisation")
    async def diary_utilisation(
        start_date: str | None = None,
        end_date: str | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        """Summarise diary load: appointments, booked minutes and cancellations per practitioner.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            site_id: Restrict to one site.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            records = await client.list_all(
                "appointments", "appointments", limit=1000,
                start_time=f"{start}T00:00:00",
                finish_time=f"{end}T23:59:59",
                site_id=site_id,
            )

        per: dict[str, dict[str, Any]] = {}
        for a in records:
            key = str(a.get("practitioner_id") or "unassigned")
            row = per.setdefault(key, {"practitioner_id": key, "appointments": 0,
                                       "booked_minutes": 0, "cancelled": 0, "completed": 0})
            state = str(a.get("state") or "").lower()
            if state == "cancelled":
                row["cancelled"] += 1
                continue
            row["appointments"] += 1
            row["booked_minutes"] += int(a.get("duration") or 0)
            if state == "completed":
                row["completed"] += 1

        return {
            "start_date": start,
            "end_date": end,
            "site_id": site_id,
            "total_appointments": sum(r["appointments"] for r in per.values()),
            "total_cancelled": sum(r["cancelled"] for r in per.values()),
            "by_practitioner": sorted(per.values(), key=lambda r: -r["booked_minutes"]),
        }


def _require_iso(value: str, field: str) -> None:
    if not value or "T" not in value:
        raise ValidationError(f"{field} must be ISO 8601, e.g. 2026-09-14T09:30:00 — got {value!r}.")


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "")}
