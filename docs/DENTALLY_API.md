# Dentally API — field notes

What the API looks like from the outside, and which parts of it this server verified
directly versus inferred. Written August 2026.

---

## Confidence

Marked throughout:

* ✅ **verified** — read directly from Dentally's published documentation
* ⚠️ **inferred** — from third-party integration write-ups, community threads and
  partner integrations. The tool exists and is coded defensively, but the exact path
  or parameter names may differ. Confirm against the partner documentation pack.

The docs page truncates when fetched programmatically, so resources alphabetically
after *Patients* could not be read at source. That is the whole reason for the
distinction — it is a limit on the research, not a judgement about Dentally.

---

## Hosts

Regionally sharded. The sandbox is a **separate host with separate credentials** — a
production token is not valid against it and vice versa.

| Region | Base URL |
|---|---|
| UK (production) | `https://api.dentally.co` |
| Sandbox | `https://api.sandbox.dentally.co` |
| APAC | `https://api.apac.dentally.com` |
| Canada | `https://api.ca.dentally.com` |

Versioned by path: `/v1/patients`. Omitting the version returns the latest, which is
not what you want in production.

---

## Authentication ✅

OAuth2, plus practice-generated API keys. Scopes look like `user:read`,
`patient:read`, `patient:update`, and are echoed back on every response in
`X-OAuth-Scopes` — which makes a scope problem diagnosable instead of a mystery 403.

**There is no refresh flow.** A token stays valid by being used and dies when idle.
This is the single most consequential fact about integrating with Dentally, and it is
why this server has a keepalive rather than a refresh loop.

---

## Required headers ✅

`User-Agent` is **mandatory**. A request without one gets **403 Forbidden**, not 400.
That status reads like a permissions problem and has cost integrators real time. This
server always sends one and, when it sees a 403, says which of the two causes it is.

CORS is supported from any origin.

---

## Limits ✅

| Limit | Value |
|---|---|
| Requests | 3,600/hour **per user** |
| Page size | 25 default, 100 maximum |
| Deep paging | Avoid past page 100 — prone to timeouts |
| Date filters | Keep large entities inside ~3 months |
| Metadata | 3 key-value pairs; keys ≤40 chars, values ≤500 |

Rate limit state comes back in response headers. The budget is **per user and
shared** — the practice's other integrations and Dentally's own app draw on the same
pool, so exhausting it can take the practice's online booking down with you.

---

## Resources

### Verified ✅

| Resource | Operations |
|---|---|
| Patients | create, get, edit, delete, list |
| Appointments | create, get, edit, delete, list, **availability** |
| Appointment reasons | list |
| Appointment cancellation reasons | list |
| Accounts | get, list |
| Invoices | get, list |
| Invoice items | list |
| Fees | get, list, edit |
| NHS claims | get, list |
| Contracts (NHS) | get, list |
| Patient referrals | get, list |
| Acquisition sources | list |
| Custom fields | get, list |

### Inferred ⚠️

Payments · Payment plans · Practitioners · Sites · Treatments · Treatment plans and
items · Users.

These appear consistently across Dentally sync tooling and partner integrations, so
they exist. The tools built on them handle a 404 or a scope refusal by returning an
empty section rather than failing the whole call — which is why
`get_patient_care_summary` degrades to a missing section instead of an error when a
practice does not use that module.

---

## Webhooks ⚠️

Confirmed events: `patient.created`, `patient.updated`, `patient.deleted`.

A known wrinkle from Dentally's own forum: the sandbox UI only offers checkboxes for
`created` and `updated`. `patient.deleted` is supported but must be subscribed via
the API.

Whether appointment and invoice events fire is an open question — asked on their
forum and, at the time of writing, unanswered. **Do not design a workflow that
depends on an appointment webhook without confirming it first.**

This server does not consume webhooks yet. When it does, the receiver must verify
signatures and be idempotent; delivery guarantees are not documented.

---

## Behaviour this server codes around

**Collections are wrapped in the resource name.** `{"patients": [...]}`, not a bare
array. Single objects are wrapped too: `{"patient": {...}}`.

**Money comes back inconsistently** — sometimes a number, sometimes a string,
sometimes with a currency symbol. Parsed forgivingly.

**Field names vary between resources** for the same concept: `total` / `gross`,
`state` / `status`, `outstanding` / `balance`, `time_zone` / `timezone`. The
projections accept either.

**Appointment state changes update timestamps automatically** — set the state and
Dentally maintains the corresponding timestamp. Do not set both.

---

## Sources

* Dentally developer documentation — <https://developer.dentally.co/>
* Dentally partner enquiry — <https://www.dentally.com/en-gb/integrations/become-a-partner>
* Dentally community, Integrations/API — <https://community.dentally.com/integrations-api-47>
* A third-party Dentally data-sync write-up (rate limits, the missing refresh flow)
  — <https://hackmd.io/@jwdunne/Hkfq2Iivs>
