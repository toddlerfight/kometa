#!/usr/bin/env bash
# Fresh-install test rig. Spins up Kometa locally with a virgin everything:
# empty DB, empty comics dir, no creds. The out-of-the-box experience,
# bottled. Runs on :6970 so it never elbows the real NAS instance.
#
#   ./local-fresh.sh          start (reuses ./local/ state from last run)
#   ./local-fresh.sh --wipe   scorched earth: nuke ./local/, start truly fresh
#   ./local-fresh.sh --down   stop and remove the container (state survives)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE=(docker compose -f "$ROOT/docker-compose.local.yml" -p kometa-local)

case "${1:-}" in
  --down)
    "${COMPOSE[@]}" down
    exit 0
    ;;
  --wipe)
    "${COMPOSE[@]}" down 2>/dev/null || true
    rm -rf "$ROOT/local"
    echo "wiped ./local — next run is a true day-zero install"
    ;;
esac

mkdir -p "$ROOT/local/data" "$ROOT/local/downloads" "$ROOT/local/comics"

"${COMPOSE[@]}" up --build -d
echo
echo "fresh Kometa: http://localhost:6970"
echo "logs:         docker logs -f kometa-local"
echo "stop:         ./local-fresh.sh --down"
