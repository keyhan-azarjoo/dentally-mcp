# Deploying

The shape: the container binds **127.0.0.1 only**, and nginx in front of it terminates
TLS. The app never listens on a public interface itself, so a misconfigured firewall
cannot expose it directly.

```
Claude / OpenAI  ──HTTPS──▶  nginx :443  ──▶  127.0.0.1:8092  (container)
                                                    │
                                                    ▼
                                            api.dentally.co
```

---

## 1. Host prerequisites

Docker, and nginx with certbot:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

You need a hostname that resolves to the box. If you do not have a domain handy,
[sslip.io](https://sslip.io) resolves any IP-embedded name to that IP with no DNS
setup at all — `dentally.203-0-113-9.sslip.io` → `203.0.113.9`. Good enough for a
pilot, and Let's Encrypt will issue for it.

---

## 2. First deploy

```bash
./deploy/deploy.sh root@your-server
```

It will stop and tell you to create `.env`. Do that on the server:

```bash
ssh root@your-server
cd /opt/dentally-mcp
cp .env.example .env
```

The three that matter:

```bash
# The token YOUR clients present. Generate: openssl rand -hex 32
DENTALLY_MCP_AUTH_TOKEN=<64 hex chars>

# Must match the public hostname exactly — it is what the DNS-rebinding allow-list
# is derived from. Get it wrong and every request fails with an opaque 400.
DENTALLY_MCP_PUBLIC_URL=https://dentally.your-host.example.com

# Encrypts stored practice tokens. Generate:
#   docker run --rm dentally-mcp:local python -m dentally_mcp.tokenstore --genkey
DENTALLY_TOKEN_KEY=<fernet key>
```

Then run the deploy again.

**A practice's Dentally token is not required to start the server.** It comes later,
through `/auth/token` or the OAuth flow — so you can stand this up and connect an AI
client before you have any practice credentials at all. Tools will return a clear
"no Dentally credential is available" until one is added.

---

## 3. nginx and TLS

```bash
sudo cp /opt/dentally-mcp/deploy/nginx-dentally-mcp.conf /etc/nginx/sites-available/dentally-mcp
sudo sed -i "s/__HOST__/dentally.your-host.example.com/g" /etc/nginx/sites-available/dentally-mcp
sudo ln -s /etc/nginx/sites-available/dentally-mcp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dentally.your-host.example.com
```

If nginx already serves other sites on that box, check for a duplicate
`map $http_upgrade` before reloading — nginx refuses to start on one, and that takes
down every site on the host, not just this one.

Two settings in that vhost are load-bearing, not tuning:

* **`proxy_buffering off`** — MCP's Streamable HTTP keeps a response open and pushes
  events down it. With buffering on, nginx holds them until the stream ends, which for
  a streaming transport never happens. The client just hangs, with no error.
* **`proxy_read_timeout 3600s`** — nginx's 60-second default cuts off a tool call
  waiting on a slow Dentally response.

---

## 4. Verify

```bash
curl https://dentally.your-host.example.com/healthz

# Must be 401 — if this returns anything else, the server is open to the internet
curl -si -X POST https://dentally.your-host.example.com/mcp | head -1

# Must be 200
curl -si -X POST https://dentally.your-host.example.com/mcp \
  -H "Authorization: Bearer $DENTALLY_MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18","capabilities":{},
        "clientInfo":{"name":"curl","version":"1"}}}' | head -1
```

Then connect a client — see [CONNECTING.md](CONNECTING.md).

---

## 5. Adding a practice

Once the practice has generated its token (Settings → Developer Settings):

```bash
curl -X POST https://dentally.your-host.example.com/auth/token \
  -H "Authorization: Bearer $DENTALLY_MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token":"<their Dentally token>","label":"Smile Dental"}'
```

The server validates it against Dentally before storing, so a bad token fails here
rather than three tool calls later. The response tells you the practice id to put in
`X-Dentally-Practice`.

---

## Updating

```bash
./deploy/deploy.sh root@your-server
```

Rebuilds and restarts. `.env`, the token store and the audit log all live outside the
image and survive.

## Backups

The Docker volume holds `tokens.enc` and `audit.log`. Back it up, and back up
`DENTALLY_TOKEN_KEY` somewhere separate — losing the key means every practice has to
re-authorise.

## Logs

```bash
ssh root@your-server 'cd /opt/dentally-mcp && docker compose logs -f --tail 100'
```
