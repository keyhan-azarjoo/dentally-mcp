#!/usr/bin/env bash
# Deploy the Dentally MCP server to a Linux host over SSH.
#
#   ./deploy/deploy.sh root@your-server [remote-dir]
#
# Ships the working tree (excluding secrets and build junk), builds the image on the
# box, and restarts the stack. The remote .env is preserved — it holds the practice
# credentials and is never overwritten by a deploy.
#
# What this deliberately does NOT do: touch nginx or issue certificates. Those are
# one-time host setup, and a deploy script that reconfigures the web server is a
# deploy script that can take unrelated sites down. See docs/DEPLOY.md.

set -euo pipefail

TARGET="${1:?usage: deploy.sh user@host [remote-dir]}"
REMOTE_DIR="${2:-/opt/dentally-mcp}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Deploying $HERE to $TARGET:$REMOTE_DIR"

ssh "$TARGET" "mkdir -p $REMOTE_DIR"

# COPYFILE_DISABLE stops macOS tar from embedding ._AppleDouble files, which break
# the build on Linux in confusing ways.
COPYFILE_DISABLE=1 tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.env' \
  --exclude='*.enc' \
  --exclude='audit.log' \
  --exclude='graphify-out' \
  -czf - -C "$HERE" . | ssh "$TARGET" "tar xzf - -C $REMOTE_DIR"

ssh "$TARGET" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_DIR"

if [ ! -f .env ]; then
  echo "!! No .env on the server. Create one from .env.example before the first run:"
  echo "   ssh $TARGET 'nano $REMOTE_DIR/.env'"
  echo "   It needs at minimum DENTALLY_MCP_AUTH_TOKEN and DENTALLY_MCP_PUBLIC_URL."
  exit 1
fi

docker compose build
docker compose up -d
sleep 3
docker compose ps
REMOTE

echo "==> Health check"
ssh "$TARGET" "curl -fsS http://127.0.0.1:8092/healthz" && echo
echo "==> Done."
