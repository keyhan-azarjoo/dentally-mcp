# Connecting the server to an AI client

Two transports, chosen by where the client runs:

* **stdio** — the client launches the server as a subprocess on the same machine.
  Claude Desktop, Claude Code, Cursor, Windsurf.
* **Streamable HTTP** — the client calls a URL. OpenAI's Responses API, Claude web
  connectors, your own backend, MyOTGO.

Same tools, same guards. The difference is only how the caller is identified.

Three headers control the HTTP transport:

| Header | Purpose |
|---|---|
| `Authorization: Bearer <DENTALLY_MCP_AUTH_TOKEN>` | Required. Your token, not Dentally's |
| `X-Dentally-Practice: <id>` | Which practice to act for. Optional with a single practice |
| `X-Dentally-Role: reception\|clinician\|nurse\|manager\|full` | Which tool set to expose |

---

## Claude Desktop / Claude Code (stdio)

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dentally": {
      "command": "python",
      "args": ["-m", "dentally_mcp"],
      "cwd": "/path/to/dentally-mcp",
      "env": {
        "PYTHONPATH": "/path/to/dentally-mcp/src",
        "DENTALLY_REGION": "sandbox",
        "DENTALLY_API_TOKEN": "your-dentally-token",
        "DENTALLY_DEFAULT_SURFACE": "reception",
        "DENTALLY_ALLOW_WRITES": "0"
      }
    }
  }
}
```

Claude Code, one line:

```bash
claude mcp add dentally -- python -m dentally_mcp
```

Claude Desktop and Claude Code both support MCP elicitation, so write confirmation
works properly there: you will be asked before anything changes in Dentally.

---

## OpenAI (Responses API, remote MCP)

OpenAI calls your server over HTTPS, so it must be publicly reachable and it must
have `DENTALLY_MCP_AUTH_TOKEN` set.

```python
from openai import OpenAI

client = OpenAI()

response = client.responses.create(
    model="gpt-5",
    input="Who is booked in with Dr Patel tomorrow, and are there any gaps?",
    tools=[{
        "type": "mcp",
        "server_label": "dentally",
        "server_url": "https://your-host.example.com/mcp",
        "headers": {
            "Authorization": f"Bearer {DENTALLY_MCP_AUTH_TOKEN}",
            "X-Dentally-Practice": "12345",
            "X-Dentally-Role": "reception",
        },
        "require_approval": "always",
    }],
)
```

**Keep `require_approval` on for any server with writes enabled.** OpenAI's remote
MCP path has no elicitation, so this server's own confirmation prompt cannot run —
which means it fails closed and refuses the write. OpenAI's approval gate is the
control that replaces it. If you set `DENTALLY_CONSENT_FALLBACK_ALLOW=1` to get past
that, you are removing the only human check on the write.

---

## Claude API / claude.ai connector

```python
import anthropic

client = anthropic.Anthropic()

message = client.beta.messages.create(
    model="claude-opus-4-5",
    max_tokens=2000,
    messages=[{"role": "user", "content": "How many recalls are due next week and unbooked?"}],
    mcp_servers=[{
        "type": "url",
        "url": "https://your-host.example.com/mcp",
        "name": "dentally",
        "authorization_token": DENTALLY_MCP_AUTH_TOKEN,
    }],
    betas=["mcp-client-2025-04-04"],
)
```

For a claude.ai connector, add the same URL under **Settings → Connectors → Add
custom connector**. The server publishes
`/.well-known/oauth-protected-resource` so the client can discover how to authorise.

---

## MyOTGO (or any app you own)

The pattern that works well: your backend holds the server token, and passes the
practice and role from the signed-in user's own session. The AI client never sees a
Dentally credential, and it cannot choose which practice to read.

```
User (signed in to your app)
  └─ your backend  ── session says: practice 12345, role "nurse"
       └─ POST https://dentally-mcp/mcp
            Authorization: Bearer <server token>     ← your secret
            X-Dentally-Practice: 12345               ← from the session
            X-Dentally-Role: nurse                   ← from the user's job role
```

Two properties worth keeping:

1. **Derive the role from the user's actual job role, never from the model.** A
   receptionist gets the reception surface because of who they are, not because a
   prompt asked for it.
2. **Derive the practice from the session, never from a tool argument.** This is what
   stops one practice's assistant reading another practice's records.

Onboarding a practice from your own UI:

```
POST /auth/login?json=1        → { authorize_url, state }   → open it for the user
   … user approves in Dentally …
GET  /auth/callback            → stores the token, shows the practice id
GET  /auth/practices           → confirm it is connected
```

---

## Choosing a role

| Role | Sees | For |
|---|---|---|
| `reception` | Diary, patients, booking, availability | Front desk. The only role that can change the diary |
| `clinician` | Patients, care summaries, treatment plans, fees | Dentists |
| `nurse` | Today's list, patients, recalls. No financial tools | Nurses, hygienists, therapists |
| `manager` | Invoices, payments, NHS claims, revenue, utilisation | Practice managers |
| `full` | Everything | Operators and integration tests |

Job titles map automatically: `dentist` → `clinician`, `hygienist` → `nurse`,
`receptionist` → `reception`, `practice-manager` → `manager`.

An unrecognised role falls back to `reception`, the narrowest useful surface — an
unknown header must never widen access.

---

## Questions each role can actually answer

Reception:
> *"Who's in this afternoon?"* · *"Find me a 30-minute slot with Dr Patel next week"*
> · *"Has Jane Doe got anything booked?"*

Clinician:
> *"Give me the care summary for patient 4821"* · *"What's outstanding on their
> treatment plan?"* · *"What do we charge for a hygiene appointment?"*

Nurse:
> *"What's on today's list for surgery 2?"* · *"Which recalls are due next week and
> not booked?"*

Manager:
> *"What did we invoice and collect last month?"* · *"Which NHS claims are stuck?"*
> · *"How's diary utilisation by practitioner this week?"*

What no role can answer, by design:
> *"What did the dentist write in the notes?"* · *"Which teeth were treated?"* ·
> *"What's their medical history?"*

Clinical detail never leaves Dentally through this server. Ask the model for it and
it will tell you so.

---

## Verifying a connection

```bash
curl https://your-host/healthz
```

Then, from the client, ask it to run `whoami`. That returns the practice, the region,
whether you are on sandbox or production, the granted scopes, whether writes are
enabled, and the exact tool list for your role. If something is misconfigured, it
shows up there rather than three tool calls later.
