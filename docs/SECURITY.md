# Security and data protection

This server connects a large language model to a UK dental practice's patient
records. That is special category personal data under UK GDPR Article 9, and the
practice — not you — carries the legal obligation for it.

Read this before enabling writes or turning redaction off.

---

## What this server does not decide for you

Running this software does not make an AI assistant lawful in a dental practice. The
practice still needs:

* a **lawful basis** under Art. 6 *and* a **condition** under Art. 9 for the
  processing;
* a **Data Protection Impact Assessment** — automated processing of health data at
  scale is close to the paradigm case for one;
* a **data processing agreement** with you, and with whoever runs the model;
* a decision, made deliberately, about whether patient data may leave the UK/EEA,
  which is a question about the *model provider*, not about this server;
* alignment with the **NHS Data Security and Protection Toolkit** if they do NHS work.

If you are selling this to practices, they will ask you for all of the above. Having
answers ready is the difference between a pilot and a procurement dead end.

---

## The controls that are actually in the code

### Clinical detail never leaves Dentally

Tooth and surface findings, charting, periodontal scores, medical history,
medication, allergies and free-text clinical notes are stripped from every response,
at every nesting depth, **regardless of configuration**. There is no setting that
turns this off.

Two reasons. Dentally's own integration guidance singles this data out as something
that should not be synced outward. And no scheduling, recall or billing question
needs it — so exposing it buys nothing and risks a great deal.

Filtering is by field-name pattern rather than an allow-list, so a field Dentally
adds next year is excluded by default instead of leaking until somebody notices.

### PII minimisation is on by default

With `DENTALLY_REDACT_PII=1` (the default):

* email addresses become `j***@example.com`
* phone numbers become `***123`
* date of birth becomes an **age**
* addresses, postcodes, NHS numbers and insurance identifiers are masked

Names survive, because *"who is my three o'clock"* is unanswerable without them.

Turning redaction off is legitimate — a receptionist confirming a phone number needs
the phone number — but it is a decision to make explicitly, with the DPIA in hand.

### Writes are off by default

`DENTALLY_ALLOW_WRITES=0` ships as the default. A read-only assistant that follows a
prompt-injected instruction can leak; one with writes can cancel a day's clinic.

When you do enable writes, every one of them asks a human to confirm the specific
action, and **fails closed when the client cannot ask**. This matters more than it
sounds: a model can be talked into booking or cancelling by text it read inside a
patient record. A human confirming *this appointment, this time* is the control that
survives that, and it only works if it cannot be skipped.

`DENTALLY_CONSENT_FALLBACK_ALLOW=1` removes that check. Only set it if the client
itself confirms writes — OpenAI's `require_approval: "always"`, for example.

### Role scoping is enforcement, not advice

`tools/list` is filtered by role, and `call_tool` re-checks the same predicate. If
only the listing were filtered, a client that cached a tool name from another session
could still invoke it, and the whole thing would be decorative.

An unrecognised role falls back to the narrowest useful surface.

### The practice is chosen by the server, never by the caller

Over HTTP, the caller names a practice; the server resolves the credential. A client
cannot present its own Dentally token. If it could, any client that obtained one
token could reach every practice that ever authorised you.

### Tokens are encrypted at rest

Practice tokens are Fernet-encrypted with `DENTALLY_TOKEN_KEY` and written `0600`
with an atomic replace. A Dentally token is a bearer credential for an entire patient
database; a stray `cat` or a backup snapshot of plaintext JSON is a reportable
breach.

The key lives in the environment and is never written to the store.

### An unauthenticated server will not start on a public interface

Binding to anything other than loopback without `DENTALLY_MCP_AUTH_TOKEN` is a hard
startup failure, not a warning. "Convenient locally" turning silently into "open on
the internet" is exactly how a patient database ends up publicly readable.

### Every tool call is audited

Timestamp, tool, practice, caller, arguments, outcome, result count. Written `0600`.

Deliberately **not** recorded: patient names, tool return values, and any argument
that looks like a credential. An audit log that duplicates the records it audits is
just a second copy of the patient database with weaker access control.

---

### Tool annotations declare what each tool does

All 25 tools carry explicit `readOnlyHint`, `destructiveHint`, `idempotentHint` and
`openWorldHint` values, so a host can warn before a call that changes the diary. A
test ties `readOnlyHint: false` to the same `WRITE_TOOLS` set the write gate enforces,
so the warning a user sees cannot drift from the control that actually applies.

### The HTML pages escape everything and carry a CSP

`/connect` collects two live credentials, so injected script there would be a
credential thief. Every interpolation is escaped, and `default-src 'none'` with
`frame-ancestors 'none'` is the backstop if an escaping bug ever returns.

---

## Known limits — read these before a multi-tenant deployment

Two things this server does NOT do. Both are deliberate, and both matter if you host
it for more than one practice.

**The server token is the whole trust boundary between clients.** Any caller holding
`DENTALLY_MCP_AUTH_TOKEN` may name any connected practice in `X-Dentally-Practice`.
There is no per-token practice allow-list. So a single shared token across several
customers means any of them can read all of them. Until that exists: issue one
deployment per customer, or put your own backend in front and never let a customer's
client hold the server token directly.

**The role header is not a defence against the client.** `X-Dentally-Role` scopes what
the *model* can see and invoke, which is its purpose — narrowing the blast radius of a
prompt injection. It does not restrain a caller that has already authenticated, since
that caller chooses the header. Derive it in your backend from the signed-in user's
job role; do not accept it from anything the user or model controls.

---

## Threat notes

**Prompt injection through patient data.** A patient note, an appointment note or a
name field can contain text aimed at the model. The mitigations here are structural,
not textual: writes off by default, per-action human confirmation, role scoping, and
never exposing free-text clinical notes in the first place. Do not rely on the system
prompt to hold that line.

**Context leakage between patients.** The tools return narrow projections rather than
whole records, and pagination is bounded, so a single call cannot pull the patient
list into a context window.

**Rate-limit exhaustion as denial of service.** The 3,600/hour budget is shared with
everything else the practice runs, including its online booking. A runaway loop in
your assistant takes their booking page down. The client-side limiter is sized under
the ceiling for that reason.

**Error reporting.** Sentry is inert without a DSN. With one, `send_default_pii=False`
is not sufficient on its own — it governs request bodies and user context, not stack
frames, and `include_local_variables` defaults to true. At the point a tool raises,
its locals hold raw Dentally patient rows, so that default would have shipped patient
data to a third party on any unhandled error. Local variables are disabled, values are
truncated, and a `before_send` hook strips request data, `extra` and frame variables.

**Model provider retention.** Everything a tool returns goes to whoever runs the
model, and may be retained under their terms. This is the single biggest data
protection question in the whole design, and this server cannot answer it for you —
choose the provider and the retention terms deliberately.

---

## Reporting a vulnerability

Email <keyhanazarjoo@gmail.com>. Please do not open a public issue for anything that
could expose patient data.
