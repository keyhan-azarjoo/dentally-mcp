# Getting access to Dentally

This is the part that takes longest, and it is not code. Read it before you build
anything on top of this server.

---

## Start here: you probably do not need partner approval

**A practice can issue its own API token, today, with no application.** Dentally
staff confirm this on their own forum: any practice you work with can generate a
read-only token from **Settings → Developer Settings**, scoped to things like
`patient:read` and `appointment:read`.

That is Mode A below, and it is enough to run this server against a real practice.

Partner approval is needed only for:

* the **sandbox** environment (a separate host with separate credentials), and
* **OAuth across many practices** — i.e. selling this as a product.

Worth knowing before you plan around it: **Dentally's partner approvals are
currently paused**, and applicants have reported months without a reply. So do not
put a launch date on the far side of one. If you need it eventually, apply early:
<https://www.dentally.com/en-gb/integrations/become-a-partner>

One catch on the self-service route: **Developer Settings requires the right Dentally
licence.** If the menu is not there, that is why — the practice needs to ask Dentally
support to enable it.

---

## Which mode do you need?

| | Mode A — API token | Mode B — OAuth |
|---|---|---|
| Practices | One | Many |
| Who holds the credential | The practice generates it in Dentally | Your OAuth app, per practice |
| Needs partner approval | **No** — self-service | Yes |
| Needs a licence with Developer Settings | Yes | — |
| Set up in | Minutes | Weeks–months (approval, currently paused) |
| Right for | A single practice, an internal tool, a pilot | A product sold to practices |

Start with Mode A. Move to Mode B only when you have more than one practice and
partner approval.

---

## Mode A — a single practice's API token

### 1. Generate the token inside Dentally

Sign in to Dentally, then go to **Settings → Developer Settings → Generate new
token**.

If you cannot see Developer Settings, the practice's licence does not include it —
ask Dentally support to enable it.

Treat the token exactly like a password: never email it, rotate it periodically, and
revoke it from that same Settings page the moment you suspect it has been shared.

**Scope it to the minimum.** The token is a bearer credential for the practice's
entire patient database; a token scoped to `patient:read` cannot be turned into a
data-deletion incident by anything downstream. Dentally exposes scopes such as:

| Scope | Grants |
|---|---|
| `user:read` | Identify the token (needed by `whoami` and by keepalive) |
| `patient:read` | Read patient records |
| `patient:update` | Edit patient records |
| `appointment:read` | Read the diary |
| `appointment:write` | Book, move and cancel appointments |

For a read-only assistant, `user:read patient:read appointment:read` is enough.

Dentally echoes the granted scopes back in the `X-OAuth-Scopes` response header, and
this server reads them — `whoami` will tell you exactly what your token can do.

### 2. Give it to the server

```bash
cp .env.example .env
# then set:
DENTALLY_REGION=sandbox
DENTALLY_API_TOKEN=<the token you just generated>
```

### 3. Prove it works before wiring anything up

```bash
python scripts/login.py --check
```

That calls `GET /v1/user` with your token and prints the practice, the user and the
granted scopes. If it fails here, it will fail identically inside an AI client, but
with a far less useful error message.

---

## Mode B — OAuth for a multi-practice product

### 1. Register an OAuth application

Dentally issues you a client id, a client secret, and the exact authorize/token URLs
during partner onboarding. Your redirect URI must match **exactly** what you
register — a trailing slash difference is enough to break the exchange.

```bash
DENTALLY_CLIENT_ID=...
DENTALLY_CLIENT_SECRET=...
DENTALLY_REDIRECT_URI=https://your-host.example.com/auth/callback
DENTALLY_SCOPES=user:read patient:read appointment:read
```

Only set `DENTALLY_AUTHORIZE_URL` / `DENTALLY_TOKEN_URL` if the URLs Dentally gives
you differ from `<api base>/oauth/authorize` and `<api base>/oauth/token`.

### 2. Set up the token store

Practice tokens are encrypted at rest — they are bearer credentials for entire
patient databases, and plaintext JSON on disk is not an acceptable place for them.

```bash
python -m dentally_mcp.tokenstore --genkey    # put the output in DENTALLY_TOKEN_KEY
```

Keep that key somewhere you will not lose it. Losing it means every practice has to
re-authorise.

### 3. Run the login flow

Send the practice to `https://your-host/auth/login`. They sign in to Dentally,
approve the scopes, and Dentally redirects to `/auth/callback`, which exchanges the
code and stores the token. The page then shows the practice id to use in the
`X-Dentally-Practice` header.

The flow uses PKCE and a one-time `state`. If Dentally's OAuth app rejects the PKCE
parameters, set `DENTALLY_OAUTH_PKCE=0`.

---

## The thing that will break your integration in three months

**Dentally does not give you a refresh token.** A token stays valid because it is
*used*, and it dies when it goes idle.

This is not a footnote. An integration that works perfectly through a busy autumn
can be silently dead after a quiet Christmas, and the failure looks like a `401`
with no explanation.

This server handles it with a keepalive: a periodic authenticated ping of every
stored token.

```bash
DENTALLY_KEEPALIVE=1
DENTALLY_KEEPALIVE_HOURS=12
```

You can also drive it externally, which is worth doing if you want the result in
your own monitoring:

```bash
curl -X POST https://your-host/auth/keepalive \
     -H "Authorization: Bearer $DENTALLY_MCP_AUTH_TOKEN"
```

A token that has died reports `reauth-required`. The record is deliberately **not**
deleted — a practice that must re-authorise needs to be told, and quietly dropping
the record would hide the outage from whoever can fix it.

---

## The REST endpoints

Everything except `/healthz`, `/auth/login` and `/auth/callback` requires the
server's own bearer token (`DENTALLY_MCP_AUTH_TOKEN`) — not Dentally's.

| Method | Path | Does |
|---|---|---|
| `GET` | `/healthz` | Liveness, region, mode, whether writes are on |
| `GET` | `/auth/login` | Start OAuth. `?json=1` returns the URL instead of redirecting |
| `GET` | `/auth/callback` | Dentally redirects here; exchanges and stores the token |
| `POST` | `/auth/token` | Register a pasted API token, after validating it |
| `POST` | `/auth/validate` | Check a token and report its scopes. Stores nothing |
| `GET` | `/auth/practices` | List connected practices (tokens masked) |
| `DELETE` | `/auth/practices/{id}` | Forget a practice's credential |
| `POST` | `/auth/keepalive` | Ping every stored token now |
| `GET` | `/.well-known/oauth-protected-resource` | RFC 9728 metadata for MCP clients |

Practice tokens are never returned by any endpoint, only ever masked.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403` on every request | No `User-Agent` header. Dentally rejects those with 403, which reads like a permissions problem and is not. This server always sends one |
| `403` on some requests | The token lacks that scope. Check `X-OAuth-Scopes`, or run `whoami` |
| `401` after a quiet period | The token died of idleness. Re-authorise, then turn keepalive on |
| Requests fail after a burst | You hit 3,600/hour. That budget is shared with everything else the practice runs |
| Sandbox token rejected in production | They are separate hosts with separate credentials. Check `DENTALLY_REGION` |
| Requests time out on big listings | Date range too wide, or paging too deep. This server clamps both |
