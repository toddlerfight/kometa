#!/usr/bin/env bash
set -euo pipefail

SSH="ssh -p 42069 -i ~/.ssh/id_ed25519 marcusg@192.168.1.166"
DOCKER="/var/packages/ContainerManager/target/usr/bin/docker"
ROOT="$(cd "$(dirname "$0")" && pwd)"

pipe() {
  cat "$ROOT/$1" | $SSH "$DOCKER exec -i kometa sh -c 'cat > /app/$1'"
  echo "  ✓ $1"
}

echo "deploying..."
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
