#!/usr/bin/env bash
# Write kometa/_build.json so the running app knows what commit it is.
#
# Standalone on purpose: there are two deploy paths (deploy.sh's docker-exec
# pipe, and the tar-sync in INSTRUCTIONS.md). A stamp that only one of them
# writes is worse than no stamp — it'd go stale silently and you'd trust it.
# Both call this. Run it before you sync anything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
DIRTY=false
[ -n "$(git -C "$ROOT" status --porcelain)" ] && DIRTY=true

cat > "$ROOT/kometa/_build.json" <<EOF
{
  "sha": "$SHA",
  "short_sha": "$(echo "$SHA" | cut -c1-7)",
  "dirty": $DIRTY,
  "branch": "$BRANCH",
  "deployed_at": $(date +%s),
  "source": "stamp"
}
EOF

if [ "$DIRTY" = true ]; then
  echo "  ⚠ tree is DIRTY — deploying uncommitted changes, no rollback point"
fi
echo "  ✓ stamped $BRANCH @ $(echo "$SHA" | cut -c1-7)"
