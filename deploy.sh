#!/usr/bin/env bash
set -euo pipefail

# Host connection details live OUTSIDE this repo — it's public.
# Put them in .env (gitignored) or export them before running:
#   KOMETA_HOST=... KOMETA_USER=... KOMETA_KEY=~/.ssh/your_key ./deploy.sh
ROOT="$(cd "$(dirname "$0")" && pwd)"
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

# NAS_* are the legacy names from when this lived on the Synology. Still honoured
# so an old .env keeps working, but KOMETA_* wins.
HOST="${KOMETA_HOST:-${NAS_HOST:?KOMETA_HOST not set — see .env.example}}"
USER_="${KOMETA_USER:-${NAS_USER:?KOMETA_USER not set — see .env.example}}"
PORT="${KOMETA_PORT:-${NAS_PORT:-22}}"
KEY="${KOMETA_KEY:-${NAS_KEY:-$HOME/.ssh/id_ed25519}}"
# Where docker-compose.yml lives on the host, and where the bind mount roots.
APP_DIR="${KOMETA_APP_DIR:?KOMETA_APP_DIR not set — see .env.example}"
COMPOSE_DIR="${KOMETA_COMPOSE_DIR:-$APP_DIR}"
DOCKER="${KOMETA_DOCKER:-docker}"

SSH="ssh -o IdentitiesOnly=yes -p $PORT -i $KEY $USER_@$HOST"

echo "deploying to $HOST:$APP_DIR ..."
"$ROOT/stamp.sh"

# Sync the whole package. Excludes: __pycache__ is root-owned by the container
# and the extract dies on it; .DS_Store is macOS being macOS. Enumerating files
# by hand is how this script rotted last time — a renamed module made it abort
# mid-deploy, after it had already overwritten main.py. Never again.
COPYFILE_DISABLE=1 tar czf - --exclude '__pycache__' --exclude '.DS_Store' \
  kometa | $SSH "cd '$APP_DIR' && tar xzf -"
echo "  ✓ synced kometa/"

echo "restarting..."
$SSH "cd '$COMPOSE_DIR' && $DOCKER compose restart kometa" > /dev/null
echo "done — verify: curl http://$HOST:6969/api/version"
