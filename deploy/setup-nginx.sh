#!/usr/bin/env bash
# One-time host setup: nginx vhost + Let's Encrypt certificate.
#
#   sudo ./deploy/setup-nginx.sh dentally.your-host.example.com you@example.com
#
# Run this ON the server, once. Deploys afterwards use deploy/deploy.sh, which
# deliberately never touches nginx — a deploy script that reconfigures the web server
# is a deploy script that can take unrelated sites down.
#
# No domain? sslip.io resolves any IP-embedded name to that IP with no DNS setup:
#   dentally.203-0-113-9.sslip.io -> 203.0.113.9
# Let's Encrypt will issue for it.

set -euo pipefail

HOST="${1:?usage: setup-nginx.sh <hostname> [email]}"
EMAIL="${2:-}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx-dentally-mcp.conf"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

echo "==> Checking the box is actually serving this name"
if ! curl -fsS "http://127.0.0.1:8092/healthz" >/dev/null; then
  echo "!! Nothing healthy on 127.0.0.1:8092. Start the container first (deploy/deploy.sh)."
  exit 1
fi

echo "==> Installing vhost for $HOST"
sed "s/__HOST__/$HOST/g" "$SRC" > /etc/nginx/sites-available/dentally-mcp

# A duplicate `map $http_upgrade ...` makes nginx refuse to start — which takes down
# EVERY site on the box, not just this one. The map here is uniquely named to avoid
# colliding with a $connection_upgrade another vhost already defines, but check the
# exact name anyway in case this script has been run before.
if grep -rqs 'connection_upgrade_dentally' /etc/nginx/sites-enabled/ 2>/dev/null; then
  if [ ! -L /etc/nginx/sites-enabled/dentally-mcp ]; then
    echo "!! Another enabled vhost already defines connection_upgrade_dentally. Resolve by hand."
    exit 1
  fi
fi

ln -sf /etc/nginx/sites-available/dentally-mcp /etc/nginx/sites-enabled/dentally-mcp
nginx -t
systemctl reload nginx
echo "==> nginx reloaded"

echo "==> Requesting a certificate"
CERTBOT_ARGS=(--nginx -d "$HOST" --non-interactive --agree-tos --redirect)
if [ -n "$EMAIL" ]; then
  CERTBOT_ARGS+=(-m "$EMAIL")
else
  CERTBOT_ARGS+=(--register-unsafely-without-email)
fi
certbot "${CERTBOT_ARGS[@]}"

echo "==> Verifying the TLS block kept the streaming settings"
# certbot clones the port-80 block. If it ever stops doing that faithfully, MCP
# clients hang with no error rather than failing loudly — so check, do not assume.
if ! awk '/listen 443/,0' /etc/nginx/sites-enabled/dentally-mcp | grep -q "proxy_buffering off"; then
  echo "!! The TLS block is missing 'proxy_buffering off'. MCP clients will hang."
  echo "   Copy the location block from the port-80 server into the 443 one."
  exit 1
fi

echo
echo "==> Done. Verify:"
echo "   curl https://$HOST/healthz"
echo "   curl -si -X POST https://$HOST/mcp | head -1     # must be 401"
