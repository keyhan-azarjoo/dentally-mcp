"""A synthetic Dentally, for demos and for testing a client end to end.

Why this exists: getting a real Dentally token needs an admin account and the right
licence, and the sandbox needs partner approval. That leaves anyone evaluating this —
or any nurse who wants to show their practice manager what it does before asking for
access — with nothing to run. Demo mode closes that gap.

It is a fake *upstream*, not a fake server. The transport below answers as Dentally
would, and everything above it is the real code path: real rate limiting, real
pagination, real redaction, real role scoping. So a demo exercises the thing you will
actually ship, and a bug in a projection shows up here rather than in front of a
practice.

Every record is invented. The names are deliberately implausible so nobody can mistake
a demo for live data.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx

PRACTICE_NAME = "DEMO PRACTICE — synthetic data, not a real practice"


def _today() -> date:
    return date.today()


def _at(day_offset: int, hour: int, minute: int = 0) -> str:
    d = _today() + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, minute).isoformat()


# Obviously-fictional people. If one of these ever appears in a real practice's
# output, something is badly wrong and it should be instantly recognisable.
PATIENTS: list[dict[str, Any]] = [
    {"id": 9001, "first_name": "Ada", "last_name": "Testwood", "date_of_birth": "1984-03-11",
     "gender": "female", "email_address": "ada@example.invalid", "mobile_phone": "07700900001",
     "site_id": 1, "active": True, "payment_plan_name": "Private"},
    {"id": 9002, "first_name": "Bram", "last_name": "Fauxley", "date_of_birth": "1997-11-02",
     "gender": "male", "email_address": "bram@example.invalid", "mobile_phone": "07700900002",
     "site_id": 1, "active": True, "payment_plan_name": "NHS"},
    {"id": 9003, "first_name": "Cleo", "last_name": "Mockridge", "date_of_birth": "2015-06-23",
     "gender": "female", "email_address": "guardian@example.invalid", "mobile_phone": "07700900003",
     "site_id": 1, "active": True, "payment_plan_name": "NHS (child)"},
    {"id": 9004, "first_name": "Dev", "last_name": "Sampleton", "date_of_birth": "1962-01-30",
     "gender": "male", "email_address": "dev@example.invalid", "mobile_phone": "07700900004",
     "site_id": 2, "active": True, "payment_plan_name": "Denplan"},
    {"id": 9005, "first_name": "Esme", "last_name": "Placeholder", "date_of_birth": "1978-09-14",
     "gender": "female", "email_address": "esme@example.invalid", "mobile_phone": "07700900005",
     "site_id": 1, "active": True, "payment_plan_name": "Private"},
]

PRACTITIONERS = [
    {"id": 501, "name": "Dr Nadia Example", "practitioner_type": "dentist", "site_id": 1, "active": True},
    {"id": 502, "name": "Dr Owen Fictitious", "practitioner_type": "dentist", "site_id": 1, "active": True},
    {"id": 503, "name": "Priya Notional", "practitioner_type": "hygienist", "site_id": 1, "active": True},
    {"id": 504, "name": "Dr Sam Invented", "practitioner_type": "dentist", "site_id": 2, "active": True},
]

SITES = [
    {"id": 1, "name": "Demo Practice — High Street", "active": True, "time_zone": "Europe/London"},
    {"id": 2, "name": "Demo Practice — Riverside", "active": True, "time_zone": "Europe/London"},
]

APPOINTMENT_REASONS = [
    {"id": 71, "name": "Check-up", "duration": 15, "active": True},
    {"id": 72, "name": "Hygiene", "duration": 30, "active": True},
    {"id": 73, "name": "Filling", "duration": 45, "active": True},
    {"id": 74, "name": "Emergency", "duration": 20, "active": True},
]

CANCELLATION_REASONS = [
    {"id": 81, "name": "Patient cancelled"},
    {"id": 82, "name": "Failed to attend"},
    {"id": 83, "name": "Practice cancelled"},
]

FEES = [
    {"id": 601, "name": "Examination", "price": "28.50", "duration": 15, "code": "EX"},
    {"id": 602, "name": "Hygiene appointment", "price": "62.00", "duration": 30, "code": "HY"},
    {"id": 603, "name": "Composite filling", "price": "145.00", "duration": 45, "code": "CF"},
    {"id": 604, "name": "Crown", "price": "650.00", "duration": 60, "code": "CR"},
]


def _appointments() -> list[dict[str, Any]]:
    """A diary spanning yesterday to next week, so date filters do something real."""
    rows = [
        (9001, 501, 0, 9, 0, 15, "active", 71),
        (9002, 503, 0, 9, 30, 30, "active", 72),
        (9005, 501, 0, 11, 0, 45, "active", 73),
        (9003, 502, 0, 14, 0, 15, "active", 71),
        (9004, 504, 0, 15, 30, 60, "cancelled", 71),
        (9002, 501, 1, 10, 0, 15, "active", 71),
        (9005, 503, 2, 9, 0, 30, "active", 72),
        (9001, 502, 4, 16, 0, 45, "active", 73),
        (9004, 504, 6, 11, 30, 60, "active", 71),
        (9003, 501, -1, 10, 0, 15, "completed", 71),
    ]
    out = []
    for i, (pid, prac, day, hh, mm, dur, state, reason) in enumerate(rows, start=1):
        start = _at(day, hh, mm)
        finish = (datetime.fromisoformat(start) + timedelta(minutes=dur)).isoformat()
        out.append({
            "id": 4000 + i, "patient_id": pid, "practitioner_id": prac,
            "start_time": start, "finish_time": finish, "duration": dur,
            "state": state, "site_id": 1 if prac != 504 else 2,
            "reason": next(r["name"] for r in APPOINTMENT_REASONS if r["id"] == reason),
            "confirmed": state == "active",
            "booked_via": "reception" if i % 3 else "online",
            "cancellation_reason_id": 82 if state == "cancelled" else None,
        })
    return out


def _invoices() -> list[dict[str, Any]]:
    rows = [(9001, "185.00", "0.00", -3), (9002, "62.00", "62.00", -2),
            (9005, "650.00", "325.00", -1), (9004, "28.50", "0.00", 0)]
    return [
        {"id": 7000 + i, "patient_id": pid, "total": total, "outstanding": out,
         "paid": f"{float(total) - float(out):.2f}",
         "created_at": (_today() + timedelta(days=day)).isoformat(),
         "state": "paid" if float(out) == 0 else "unpaid", "number": f"INV-{7000 + i}"}
        for i, (pid, total, out, day) in enumerate(rows, start=1)
    ]


def _payments() -> list[dict[str, Any]]:
    rows = [(9001, "185.00", -3), (9005, "325.00", -1), (9004, "28.50", 0)]
    return [
        {"id": 8000 + i, "patient_id": pid, "amount": amt,
         "created_at": (_today() + timedelta(days=day)).isoformat(), "state": "complete"}
        for i, (pid, amt, day) in enumerate(rows, start=1)
    ]


def _treatment_plans() -> list[dict[str, Any]]:
    return [
        {"id": 3001, "patient_id": 9005, "name": "Crown — upper right", "state": "accepted",
         "total": "650.00", "outstanding": "325.00", "practitioner_id": 501,
         "created_at": (_today() - timedelta(days=20)).isoformat(),
         # Deliberately present: proves the redaction layer strips it rather than
         # relying on Dentally never sending it.
         "treatment_plan_items": [{"id": 1, "tooth": 14, "surface": "MOD", "price": "650.00"}]},
        {"id": 3002, "patient_id": 9001, "name": "Two fillings", "state": "proposed",
         "total": "290.00", "outstanding": "290.00", "practitioner_id": 502,
         "created_at": (_today() - timedelta(days=5)).isoformat(),
         "treatment_plan_items": [{"id": 2, "tooth": 26, "surface": "O", "price": "145.00"},
                                  {"id": 3, "tooth": 36, "surface": "O", "price": "145.00"}]},
    ]


def _accounts() -> list[dict[str, Any]]:
    return [{"id": 2000 + p["id"] % 100, "patient_id": p["id"],
             "balance": "0.00" if p["id"] != 9005 else "325.00",
             "outstanding": "0.00" if p["id"] != 9005 else "325.00"} for p in PATIENTS]


def _nhs_claims() -> list[dict[str, Any]]:
    return [
        {"id": 5001, "patient_id": 9002, "state": "submitted", "band": "Band 1", "uda": 1.0,
         "contract_id": 11, "date_of_acceptance": (_today() - timedelta(days=9)).isoformat(),
         "submitted_at": (_today() - timedelta(days=8)).isoformat()},
        {"id": 5002, "patient_id": 9003, "state": "pending", "band": "Band 2", "uda": 3.0,
         "contract_id": 11, "date_of_acceptance": (_today() - timedelta(days=2)).isoformat()},
    ]


def _paginate(rows: list[dict], request: httpx.Request) -> list[dict]:
    q = request.url.params
    try:
        page = max(1, int(q.get("page", 1)))
        per_page = max(1, min(100, int(q.get("per_page", 25))))
    except ValueError:
        page, per_page = 1, 25
    start = (page - 1) * per_page
    return rows[start:start + per_page]


def _filter_by_param(rows: list[dict], request: httpx.Request, param: str, field: str) -> list[dict]:
    value = request.url.params.get(param)
    if value in (None, ""):
        return rows
    return [r for r in rows if str(r.get(field)) == str(value)]


def _in_window(rows: list[dict], request: httpx.Request, field: str) -> list[dict]:
    """Honour the date window the tools send, so clamping is visibly exercised."""
    q = request.url.params
    lo = q.get("start_time") or q.get("created_after")
    hi = q.get("finish_time") or q.get("created_before")
    if not lo and not hi:
        return rows

    def key(r):
        return str(r.get(field) or "")[:10]

    out = rows
    if lo:
        out = [r for r in out if key(r) >= str(lo)[:10]]
    if hi:
        out = [r for r in out if key(r) <= str(hi)[:10]]
    return out


def handler(request: httpx.Request) -> httpx.Response:  # noqa: C901 - a router is a router
    path = request.url.path
    q = request.url.params

    def ok(payload: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            # Mirror the headers the real API sets, so scope reporting and the rate
            # limiter are exercised rather than bypassed in demo mode.
            headers={
                "X-OAuth-Scopes": "user:read patient:read appointment:read",
                "X-RateLimit-Remaining": "3400",
            },
        )

    if path.endswith("/user"):
        return ok({"user": {"id": 1, "name": "Demo User", "practice_name": PRACTICE_NAME,
                            "practice_id": "demo", "site_id": 1,
                            "email": "demo@example.invalid"}})

    if "/appointments/availability" in path:
        slots = []
        base = date.fromisoformat(str(q.get("start_time", _today().isoformat()))[:10])
        try:
            duration = int(q.get("duration", 30))
        except ValueError:
            duration = 30
        for day in range(0, 5):
            for hour in (9, 10, 14, 16):
                d = base + timedelta(days=day)
                if d.weekday() >= 5:
                    continue
                start = datetime(d.year, d.month, d.day, hour)
                slots.append({
                    "start_time": start.isoformat(),
                    "finish_time": (start + timedelta(minutes=duration)).isoformat(),
                    "practitioner_id": int(q.get("practitioner_id") or 501),
                    "site_id": int(q.get("site_id") or 1),
                    "duration": duration,
                })
        return ok({"availability": slots[:20]})

    if "/appointments/" in path:
        wanted = path.rsplit("/", 1)[-1]
        for a in _appointments():
            if str(a["id"]) == wanted:
                return ok({"appointment": a})
        return httpx.Response(404, json={"error": "not found"})

    if path.endswith("/appointments"):
        rows = _appointments()
        rows = _filter_by_param(rows, request, "patient_id", "patient_id")
        rows = _filter_by_param(rows, request, "practitioner_id", "practitioner_id")
        rows = _filter_by_param(rows, request, "site_id", "site_id")
        rows = _filter_by_param(rows, request, "state", "state")
        rows = _in_window(rows, request, "start_time")
        return ok({"appointments": _paginate(rows, request)})

    if "/patients/" in path:
        wanted = path.rsplit("/", 1)[-1]
        for p in PATIENTS:
            if str(p["id"]) == wanted:
                return ok({"patient": p})
        return httpx.Response(404, json={"error": "not found"})

    if path.endswith("/patients"):
        rows = list(PATIENTS)
        search = (q.get("search") or "").strip().lower()
        if search:
            rows = [p for p in rows
                    if search in f"{p['first_name']} {p['last_name']}".lower()]
        rows = _filter_by_param(rows, request, "site_id", "site_id")
        # The recall tools pass a recall window; return everyone so the "due but
        # unbooked" cross-reference has something to actually exclude.
        return ok({"patients": _paginate(rows, request)})

    for suffix, rows in (
        ("/practitioners", PRACTITIONERS),
        ("/sites", SITES),
        ("/appointment_reasons", APPOINTMENT_REASONS),
        ("/appointment_cancellation_reasons", CANCELLATION_REASONS),
        ("/fees", FEES),
    ):
        if path.endswith(suffix):
            key = suffix.lstrip("/")
            return ok({key: _paginate(list(rows), request)})

    if path.endswith("/invoices"):
        rows = _in_window(_filter_by_param(_invoices(), request, "patient_id", "patient_id"),
                          request, "created_at")
        return ok({"invoices": _paginate(rows, request)})

    if path.endswith("/payments"):
        rows = _in_window(_filter_by_param(_payments(), request, "patient_id", "patient_id"),
                          request, "created_at")
        return ok({"payments": _paginate(rows, request)})

    if path.endswith("/accounts"):
        rows = _filter_by_param(_accounts(), request, "patient_id", "patient_id")
        return ok({"accounts": _paginate(rows, request)})

    if path.endswith("/treatment_plans"):
        rows = _filter_by_param(_treatment_plans(), request, "patient_id", "patient_id")
        return ok({"treatment_plans": _paginate(rows, request)})

    if path.endswith("/nhs_claims"):
        rows = _in_window(_nhs_claims(), request, "date_of_acceptance")
        return ok({"nhs_claims": _paginate(rows, request)})

    # Writes: accept and echo, so a demo can exercise the confirmation flow without
    # anything being persisted anywhere.
    if request.method in ("POST", "PUT"):
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        payload = next(iter(body.values()), {}) if isinstance(body, dict) else {}
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload.setdefault("id", 99999)
        key = "appointment" if "appointment" in path else "patient"
        return ok({key: payload})

    return httpx.Response(404, json={"error": f"demo has no route for {path}"})


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
