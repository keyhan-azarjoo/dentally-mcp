"""Invoices, payments, patient accounts, NHS claims and revenue."""
from __future__ import annotations

from typing import Any

from .. import redaction
from ..client import clamp_date_window
from ..errors import DentallyError
from ..surfaces import hints
from ..runtime import tool, use_client


def register(mcp) -> None:

    @mcp.tool(annotations=hints("list_invoices"))
    @tool("list_invoices")
    async def list_invoices(
        start_date: str | None = None,
        end_date: str | None = None,
        patient_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List invoices in a date range, optionally for one patient.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            patient_id: Restrict to one patient.
            state: Invoice state, e.g. 'paid', 'unpaid'.
            limit: Maximum invoices to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            records = await client.list_all(
                "invoices", "invoices",
                limit=max(1, min(int(limit), 200)),
                created_after=start, created_before=end,
                patient_id=patient_id, state=state,
                sort_by="created_at", sort_direction="desc",
            )
        return redaction.project(records, redaction.money_summary)

    @mcp.tool(annotations=hints("list_payments"))
    @tool("list_payments")
    async def list_payments(
        start_date: str | None = None,
        end_date: str | None = None,
        patient_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List payments taken in a date range.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            patient_id: Restrict to one patient.
            limit: Maximum payments to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            records = await client.list_all(
                "payments", "payments",
                limit=max(1, min(int(limit), 200)),
                created_after=start, created_before=end,
                patient_id=patient_id,
                sort_by="created_at", sort_direction="desc",
            )
        return redaction.project(records, redaction.money_summary)

    @mcp.tool(annotations=hints("get_patient_account"))
    @tool("get_patient_account")
    async def get_patient_account(patient_id: str) -> dict[str, Any]:
        """A patient's financial account: balance and what is outstanding.

        Args:
            patient_id: The Dentally patient ID.
        """
        async with use_client() as client:
            records = await client.list_all("accounts", "accounts", limit=10, patient_id=patient_id)
        if not records:
            raise DentallyError(f"No account found for patient {patient_id}.")
        return redaction.scrub({
            "patient_id": patient_id,
            "accounts": [redaction.money_summary(a) for a in records],
        })

    @mcp.tool(annotations=hints("list_nhs_claims"))
    @tool("list_nhs_claims")
    async def list_nhs_claims(
        start_date: str | None = None,
        end_date: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List NHS claims and their submission state.

        Useful for spotting claims stuck before submission, which is money the
        practice has earned but not billed.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
            state: Claim state to filter by.
            limit: Maximum claims to return.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            records = await client.list_all(
                "nhs_claims", "nhs_claims",
                limit=max(1, min(int(limit), 200)),
                created_after=start, created_before=end, state=state,
            )
        out = []
        for c in records:
            out.append({k: v for k, v in {
                "id": c.get("id"),
                "patient_id": c.get("patient_id"),
                "state": c.get("state") or c.get("status"),
                "contract_id": c.get("contract_id"),
                "band": c.get("band") or c.get("treatment_band"),
                "udas": c.get("uda") or c.get("udas"),
                "date_of_acceptance": c.get("date_of_acceptance"),
                "submitted_at": c.get("submitted_at"),
            }.items() if v is not None})
        return redaction.scrub(out)

    @mcp.tool(annotations=hints("revenue_summary"))
    @tool("revenue_summary")
    async def revenue_summary(
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Total invoiced, total collected and outstanding balance for a period.

        Computed from invoices and payments rather than a reporting endpoint, so the
        numbers are a fast operational read — reconcile against Dentally's own reports
        before using them for accounts.

        Args:
            start_date: YYYY-MM-DD. Defaults to today.
            end_date: YYYY-MM-DD. Defaults to start_date.
        """
        start, end = clamp_date_window(start_date, end_date)
        async with use_client() as client:
            invoices = await client.list_all(
                "invoices", "invoices", limit=1000,
                created_after=start, created_before=end)
            payments = await client.list_all(
                "payments", "payments", limit=1000,
                created_after=start, created_before=end)

        invoiced = sum(_money(i.get("total") or i.get("gross")) for i in invoices)
        collected = sum(_money(p.get("amount") or p.get("total")) for p in payments)
        outstanding = sum(_money(i.get("outstanding") or i.get("balance")) for i in invoices)
        return {
            "start_date": start,
            "end_date": end,
            "invoice_count": len(invoices),
            "payment_count": len(payments),
            "invoiced_total": round(invoiced, 2),
            "collected_total": round(collected, 2),
            "outstanding_total": round(outstanding, 2),
            "note": "Derived from invoice and payment records; reconcile against "
                    "Dentally's own reporting before using for accounting.",
        }

    @mcp.tool(annotations=hints("get_fees"))
    @tool("get_fees")
    async def get_fees(query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Look up treatment fees and their default appointment durations.

        Args:
            query: Filter by treatment name, e.g. 'hygiene', 'crown'.
            limit: Maximum fees to return.
        """
        async with use_client() as client:
            records = await client.list_all("fees", "fees", limit=max(1, min(int(limit), 200)))
        if query:
            needle = query.lower()
            records = [f for f in records if needle in str(f.get("name") or "").lower()]
        return [
            {k: v for k, v in {
                "id": f.get("id"),
                "name": f.get("name"),
                "price": f.get("price") or f.get("gross_price"),
                "duration_minutes": f.get("duration"),
                "code": f.get("code"),
            }.items() if v is not None}
            for f in records[:limit]
        ]


def _money(value: Any) -> float:
    """Dentally returns money as a number, a string, or minor units. Be forgiving."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        cleaned = "".join(c for c in str(value) if c.isdigit() or c in ".-")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
