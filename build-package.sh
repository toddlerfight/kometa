#!/usr/bin/env bash
# Regenerate the distributable fresh-install tarball from the current repo.
#
# Assembles: the live kometa/ source + Dockerfile + requirements.txt + the
# distribution files in packaging/ (run.sh, compose, README) into a single
# self-contained bundle a stranger can untar and `./run.sh`.
#
#   ./build-package.sh                 → ~/Desktop/kometa-fresh-install.tar.gz
#   ./build-package.sh /path/out.tgz   → custom output path
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HOME/Desktop/kometa-fresh-install.tar.gz}"
STAGE="$(mktemp -d)"
PKG="$STAGE/kometa-fresh-install"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$PKG"

# App source — exclude caches and OS cruft so the build context stays lean.
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  "$ROOT/kometa" "$PKG/"

# Build recipe (shared with the NAS image) + the distribution wrappers.
cp "$ROOT/Dockerfile" "$ROOT/requirements.txt" "$PKG/"
cp "$ROOT/packaging/docker-compose.local.yml" \
   "$ROOT/packaging/run.sh" \
   "$ROOT/packaging/README.md" "$PKG/"
chmod +x "$PKG/run.sh"

# Guard: nothing private or dead should ever ship.
if grep -rqE "192\.168|/volume1|comicvine|cleanup_tpbs" "$PKG/kometa" 2>/dev/null; then
  echo "✗ refused: private data or dead code found in source — aborting" >&2
  grep -rlE "192\.168|/volume1|comicvine|cleanup_tpbs" "$PKG/kometa" >&2
  exit 1
fi

tar czf "$OUT" -C "$STAGE" kometa-fresh-install
echo "✓ built $OUT ($(du -h "$OUT" | cut -f1)) from $(git -C "$ROOT" rev-parse --short HEAD)"
