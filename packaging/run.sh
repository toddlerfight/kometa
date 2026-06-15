#!/usr/bin/env bash
# Kometa — run locally. Works on macOS and Linux (Docker is the only requirement).
#
#   ./run.sh          start (state persists in ./local/ between runs)
#   ./run.sh --wipe   delete all local state, next start is factory-fresh
#   ./run.sh --down   stop the container (state survives)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# Run the container as you, not root — keeps ./local files owned by your user.
export PUID="$(id -u)" PGID="$(id -g)"
COMPOSE=(docker compose -f "$ROOT/docker-compose.local.yml")

case "${1:-}" in
  --down)
    "${COMPOSE[@]}" down
    exit 0
    ;;
  --wipe)
    "${COMPOSE[@]}" down 2>/dev/null || true
    # Plain rm works because files are host-owned; the container fallback covers
    # the case where an earlier run was started with sudo (root-owned files).
    rm -rf "$ROOT/local" 2>/dev/null || docker run --rm -v "$ROOT/local:/x" busybox rm -rf /x
    echo "wiped ./local — next run is factory-fresh"
    ;;
esac

mkdir -p "$ROOT/local/data" "$ROOT/local/downloads" "$ROOT/local/comics"
"${COMPOSE[@]}" up --build -d
echo
echo "Kometa is running  →  http://localhost:6970"
echo "  stop:  ./run.sh --down      logs:  docker logs -f kometa"
