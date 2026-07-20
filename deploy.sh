#!/usr/bin/env bash
set -euo pipefail

# NAS connection details live OUTSIDE this repo — it's public.
# Put them in .env (gitignored) or export them before running:
#   NAS_HOST=... NAS_PORT=... NAS_USER=... NAS_KEY=~/.ssh/your_key ./deploy.sh
ROOT="$(cd "$(dirname "$0")" && pwd)"
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

NAS_HOST="${NAS_HOST:?NAS_HOST not set — see .env.example}"
NAS_USER="${NAS_USER:?NAS_USER not set — see .env.example}"
NAS_PORT="${NAS_PORT:-22}"
NAS_KEY="${NAS_KEY:-$HOME/.ssh/id_ed25519}"

SSH="ssh -p $NAS_PORT -i $NAS_KEY $NAS_USER@$NAS_HOST"
DOCKER="/var/packages/ContainerManager/target/usr/bin/docker"

pipe() {
  cat "$ROOT/$1" | $SSH "$DOCKER exec -i kometa sh -c 'cat > /app/$1'"
  echo "  ✓ $1"
}

echo "deploying..."
"$ROOT/stamp.sh"
pipe kometa/_build.json
pipe kometa/main.py
pipe kometa/db.py
pipe kometa/metron_client.py
pipe kometa/downloader.py
pipe kometa/locg_client.py
pipe kometa/comicvine_client.py
pipe kometa/getcomics_client.py
pipe kometa/usenet_client.py
pipe kometa/sabnzbd_client.py
pipe kometa/static/app.js
pipe kometa/static/style.css
pipe kometa/static/index.html

echo "restarting..."
$SSH "$DOCKER restart kometa" > /dev/null
echo "done"
