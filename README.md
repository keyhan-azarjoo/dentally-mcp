# Dentally MCP

An [MCP](https://modelcontextprotocol.io) server for
[Dentally](https://www.dentally.com/), the UK cloud dental practice management
system. It lets a receptionist, dentist, nurse or practice manager ask an AI
assistant about their diary, their patients, their recalls and their billing — and,
when explicitly enabled, book and move appointments.

Works with **Claude**, **OpenAI**, and any app of your own over HTTP.

> **Status: functional, not yet run against production Dentally.** Every tool is
> implemented and tested against a mocked API; the resources marked ⚠️ in
> [docs/DENTALLY_API.md](docs/DENTALLY_API.md) still need confirming against the live
> API. Dentally API access requires partner approval — see
> [docs/LOGIN.md](docs/LOGIN.md) before planning around this.

---

## What it can answer

| Role | Ask it |
|---|---|
| **Reception** | *"Who's in this afternoon?"* · *"Find a 30-minute slot with Dr Patel next week"* · *"Book Jane Doe in for Thursday at 10"* |
| **Dentist** | *"Care summary for patient 4821"* · *"What's outstanding on their plan?"* · *"What do we charge for hygiene?"* |
| **Nurse / hygienist** | *"What's on today's list for surgery 2?"* · *"Which recalls are due next week and not booked?"* |
| **Practice manager** | *"What did we invoice and collect last month?"* · *"Which NHS claims are stuck?"* · *"Diary utilisation by practitioner?"* |

And what it will refuse, by design:

> *"What did the dentist write in the notes?"* · *"Which teeth were treated?"* ·
> *"What's their medical history?"*

Clinical detail never leaves Dentally through this server. There is no setting that
changes that — see [why](#design-decisions-worth-knowing-about).

---

## Quick start

```bash
git clone https://github.com/keyhan-azarjoo/dentally-mcp
cd dentally-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # set DENTALLY_API_TOKEN, keep DENTALLY_REGION=sandbox
python scripts/login.py --check
```

`--check` calls Dentally with your token and prints the practice, the user and the
granted scopes. Do this before wiring up any AI client — a credential problem fails
here with a message that tells you what to fix, rather than surfacing later as an
unexplained 403.

Then add it to Claude Code:

```bash
claude mcp add dentally -- python -m dentally_mcp
```

or run the HTTP transport for a hosted setup:

```bash
python -m dentally_mcp --http        # then: curl localhost:8092/healthz
```

Full instructions per client — Claude Desktop, Claude Code, OpenAI Responses API,
the Claude API, and your own app — are in [docs/CONNECTING.md](docs/CONNECTING.md).

---

## Getting Dentally access

**Dentally's API is not open.** It is available to approved integration partners
only, and practices on Dentally's own forum have reported waiting months for a reply
to the partner form. Apply early, develop against the sandbox, and do not schedule a
launch behind an approval you do not control.

Both credential modes are supported:

* **API token** — a single practice generates one in Dentally itself. Minutes to set
  up. Right for one practice, an internal tool, or a pilot.
* **OAuth2** — the full authorization-code flow with PKCE, for a product sold to many
  practices. Needs partner approval.

[docs/LOGIN.md](docs/LOGIN.md) covers both, the scopes to ask for, and the endpoints
this server exposes for driving login from your own UI.

---

## The 25 tools

| | |
|---|---|
| **Patients** | `search_patients` · `get_patient` · `get_patient_care_summary` · `list_treatment_plans` · `register_patient`\* · `update_patient`\* |
| **Diary** | `list_appointments` · `get_appointment` · `find_appointment_slots` · `book_appointment`\* · `reschedule_appointment`\* · `cancel_appointment`\* · `list_appointment_reasons` · `list_cancellation_reasons` · `diary_utilisation` |
| **Money** | `list_invoices` · `list_payments` · `get_patient_account` · `list_nhs_claims` · `revenue_summary` · `get_fees` |
| **Practice** | `whoami` · `list_practitioners` · `list_sites` · `list_recalls_due` |

\* writes — off by default, and each one asks a human to confirm.

Roles see different subsets. A nurse gets no financial tools; only reception can
change the diary. Set it with the `X-Dentally-Role` header or
`DENTALLY_DEFAULT_SURFACE`.

---

## Design decisions worth knowing about

**Clinical detail is stripped unconditionally.** Tooth and surface findings, perio
scores, medical history, medication, allergies, free-text notes — filtered from every
response at every depth, with no setting to disable it. Dentally's own integration
guidance flags this data as something that should not be synced outward, and no
scheduling or billing question needs it. Filtering is by field-name pattern rather
than an allow-list, so a field Dentally adds next year is excluded by default rather
than leaking until somebody notices.

**Writes are off by default and fail closed.** A read-only assistant that follows a
prompt-injected instruction can leak; one with writes can cancel a day's clinic. When
enabled, each write asks the human to confirm the specific action — and refuses when
the client has no way to ask. A model can be talked into booking by text it read
inside a patient record; a human confirming *this appointment, this time* is the
control that survives that.

**Role scoping is enforced, not advertised.** `tools/list` is filtered by role and
`call_tool` re-checks the same predicate. Filtering the listing alone would be
decorative — a client that cached a tool name from another session could still invoke
it.

**The server picks the practice, never the caller.** Over HTTP a client names a
practice; it cannot present its own Dentally token. Otherwise one leaked token would
reach every practice that ever authorised you.

**Rate limiting happens before the request, not after the 429.** Dentally's
3,600/hour budget is *per user and shared* with everything else the practice runs —
including their online booking. A runaway pagination loop in your assistant takes
their booking page down. Pagination is bounded for the same reason: an assistant that
quietly pulls 40,000 patients into a context window is a data-protection incident,
not a thorough answer.

**Keepalive, because Dentally has no refresh flow.** A token stays valid by being
*used* and dies when idle. An integration that works through a busy autumn can be
silently dead after a quiet Christmas. A dead token reports `reauth-required` and the
record is deliberately kept — quietly dropping it would hide the outage from whoever
can fix it.

**An unauthenticated server will not start on a public interface.** Hard startup
failure, not a warning.

[docs/SECURITY.md](docs/SECURITY.md) has the rest, including the UK GDPR obligations
this software does *not* discharge for you.

---

## Configuration

Everything is environment variables; see [.env.example](.env.example). The ones that
matter most:

| Variable | Default | |
|---|---|---|
| `DENTALLY_REGION` | `sandbox` | `uk`, `sandbox`, `apac`, `ca` |
| `DENTALLY_API_TOKEN` | — | Single-practice credential |
| `DENTALLY_CLIENT_ID` / `_SECRET` | — | OAuth app, for multi-practice |
| `DENTALLY_MCP_AUTH_TOKEN` | — | The token **your** clients present. Required for a non-loopback bind |
| `DENTALLY_TOKEN_KEY` | — | Fernet key encrypting stored practice tokens |
| `DENTALLY_ALLOW_WRITES` | `0` | Writes off by default |
| `DENTALLY_REQUIRE_CONSENT` | `1` | Confirm each write; fails closed |
| `DENTALLY_REDACT_PII` | `1` | Mask contact details, DOB → age |
| `DENTALLY_DEFAULT_SURFACE` | `reception` | Role when the caller names none |
| `DENTALLY_KEEPALIVE` | `1` | Keep idle tokens from dying |

---

## Development

```bash
pytest              # 88 tests
python scripts/smoke.py    # proves the HTTP transport starts and refuses anonymous callers
```

Docker:

```bash
docker compose up -d       # loopback-bound; put TLS in front before exposing it
```

Layout:

```
src/dentally_mcp/
  client.py       Dentally HTTP client — the mandatory User-Agent, rate limit, paging
  ratelimit.py    Token bucket + adoption of Dentally's own headers
  oauth.py        Authorization-code flow, token validation, keepalive
  tokenstore.py   Fernet-encrypted per-practice tokens
  auth.py         Credential resolution, client auth gate, write gate
  redaction.py    Data minimisation
  surfaces.py     Role → tool set
  runtime.py      The wrapper every tool goes through
  http_api.py     Login and practice-admin REST endpoints
  server.py       stdio + Streamable HTTP transports
  tools/          patients · appointments · finance · practice
```

---

## Related

* [Dentally developer docs](https://developer.dentally.co/)
* [Dentally partner enquiry](https://www.dentally.com/en-gb/integrations/become-a-partner)
* [Model Context Protocol](https://modelcontextprotocol.io)

Not affiliated with or endorsed by Dentally or Henry Schein One.

MIT licensed.
