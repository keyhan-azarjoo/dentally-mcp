"""The practice itself: identity, sites, practitioners, recalls."""
from __future__ import annotations

from typing import Any

from .. import auth, config, redaction, surfaces
from ..client import clamp_date_window
from ..surfaces import hints
from ..runtime import RESOLVER, tool, use_client


def register(mcp) -> None:

    @mcp.tool(annotations=hints("whoami"))
    @tool("whoami")
    async def whoami() -> dict[str, Any]:
        """Confirm which Dentally practice this session is connected to, and what it may do.

        Call this first when anything is unclear: it proves the credential works,
        names the practice, and lists the tools available to the current role.
        """
        cred = RESOLVER.resolve()
        async with use_client() as client:
            user = await client.whoami()
            observed = client.scopes
        role = surfaces.normalise(auth.current_surface.get())
        return {
            "practice_id": cred.practice_id,
            "practice_name": cred.practice_name or user.get("practice_name"),
            "user": user.get("name") or user.get("full_name"),
            "region": config.REGION,
            "environment": ("DEMO — every record below is invented; this is NOT a real "
                            "practice and NOT real patient data"
                            if config.DEMO
                            else "sandbox" if config.IS_SANDBOX else "production"),
            "role": role,
            "scopes": observed or cred.scopes,
            "writes_enabled": config.ALLOW_WRITES,
            "pii_redacted": config.REDACT_PII,
            "available_tools": sorted(surfaces.visible(role)),
        }

    @mcp.tool(annotations=hints("list_practitioners"))
    @tool("list_practitioners")
    async def list_practitioners(site_id: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        """List the practice's dentists, hygienists and therapists.

        Args:
            site_id: Restrict to one site.
            active_only: Exclude practitioners marked inactive.
        """
        async with use_client() as client:
            records = await client.list_all(
                "practitioners", "practitioners", limit=200,
                site_id=site_id, active=True if active_only else None)
        return [
            {k: v for k, v in {
                "id": p.get("id"),
                "name": p.get("name") or p.get("full_name"),
                "type": p.get("practitioner_type") or p.get("type"),
                "site_id": p.get("site_id"),
                "active": p.get("active"),
            }.items() if v is not None}
            for p in records
        ]

    @mcp.tool(annotations=hints("list_sites"))
    @tool("list_sites")
    async def list_sites() -> list[dict[str, Any]]:
        """List the practice's sites (locations). Needed for multi-site groups."""
        async with use_client() as client:
            records = await client.list_all("sites", "sites", limit=100)
        return redaction.scrub([
            {k: v for k, v in {
                "id": s.get("id"),
                "name": s.get("name"),
                "active": s.get("active"),
                "timezone": s.get("time_zone") or s.get("timezone"),
            }.items() if v is not None}
            for s in records
        ])

    @mcp.tool(annotations=hints("list_recalls_due"))
    @tool("list_recalls_due")
    async def list_recalls_due(
        start_date: str | None = None,
        end_date: str | None = None,
        site_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Patients whose recall falls in a window and who have nothing booked.

        This is the "fill the diary" list: a recall that is due but unbooked is a
        patient who will otherwise silently lapse.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            site_id: Restrict to one site.
            limit: Maximum patients to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        limit = max(1, min(int(limit), 200))

        async with use_client() as client:
            due = await client.list_all(
                "patients", "patients", limit=limit,
                recall_date_from=start, recall_date_to=end,
                site_id=site_id, active=True)

            # Cross-reference the forward diary so already-booked patients drop out.
            # Without this the list reads as "chase all of these", which wastes the
            # front desk's time and annoys patients who are already coming in.
            booked_ids: set[str] = set()
            if due:
                upcoming = await client.list_all(
                    "appointments", "appointments", limit=1000,
                    start_time=f"{start}T00:00:00",
                    finish_time=f"{end}T23:59:59",
                    site_id=site_id, state="active")
                booked_ids = {str(a.get("patient_id")) for a in upcoming}

        unbooked = [p for p in due if str(p.get("id")) not in booked_ids]
        return {
            "start_date": start,
            "end_date": end,
            "due_total": len(due),
            "already_booked": len(due) - len(unbooked),
            "patients": redaction.project(unbooked, redaction.patient_summary),
        }
