#!/bin/bash
# Nightly backup of everything Kometa needs, onto the Voot II drive plugged into
# ginbako. Run by launchd (ops/dev.kometa.backup.plist) at 04:00 — after Kometa's
# own 03:30 copy to the NAS, so the two never share the disks.
#
#   Voot II/Kometa-Backup/
#     db/kometa-YYYYmmdd-HHMM.db   a consistent copy of the live DB (sqlite .backup), 14 kept
#     covers/                      mirror of Kometa's cover store
#     docker/                      mirror of ~/docker (compose, .env, app code, Komga config) — no page cache
#     comics/                      mirror of the comics library (🧳/Comics on the NAS)
#     deleted/YYYY-mm-dd/...       anything the mirrors removed or replaced, kept 30 days
#     status.json, backup.log      the last run, and every run
#
# If the drive isn't there the run stops — it must never fill ginbako's own disk
# under a /Volumes path that happens to be empty. COMICS=0 skips the big mirror.
set -uo pipefail

DEST="/Volumes/Voot II/Kometa-Backup"
DB="$HOME/docker/apps/kometa/data/kometa.db"
COVERS="$HOME/docker/apps/kometa/data/covers/"
DOCKER="$HOME/docker/"
COMICS_SRC="/Volumes/🧳/Comics/"
KEEP_DB=14
KEEP_DELETED_DAYS=30
STAMP=$(date +%Y%m%d-%H%M)
DAY=$(date +%Y-%m-%d)
LOG="$DEST/backup.log"
ok=1; notes=()

say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# the drive, mounted for real (a mount point, not a stray folder on the boot disk)
if ! mount | grep -q " on /Volumes/Voot II "; then
  mkdir -p "$HOME/Library/Logs"
  echo "$(date '+%F %T') Voot II not mounted — backup skipped" >> "$HOME/Library/Logs/kometa-backup.log"
  exit 1
fi
mkdir -p "$DEST/db" "$DEST/covers" "$DEST/docker" "$DEST/comics" "$DEST/deleted/$DAY"
say "--- start (comics=${COMICS:-1})"

# 1. database: an online-safe copy, then prove it opens and is whole
if /usr/bin/sqlite3 "$DB" ".backup '$DEST/db/kometa-$STAMP.db'"; then
  chk=$(/usr/bin/sqlite3 "$DEST/db/kometa-$STAMP.db" "PRAGMA integrity_check;" 2>&1 | head -1)
  books=$(/usr/bin/sqlite3 "$DEST/db/kometa-$STAMP.db" "SELECT COUNT(*) FROM books;" 2>&1)
  if [ "$chk" = "ok" ]; then say "db ok ($books books)"; else ok=0; notes+=("db integrity: $chk"); say "db FAILED integrity: $chk"; fi
  ls -1t "$DEST"/db/kometa-*.db 2>/dev/null | tail -n +$((KEEP_DB + 1)) | while read -r f; do rm -f "$f"; done
else
  ok=0; notes+=("db copy failed"); say "db copy FAILED"
fi

mirror() {   # name src dst [extra rsync args...]
  local name=$1 src=$2 dst=$3; shift 3
  if /usr/bin/rsync -a --delete --backup --backup-dir="$DEST/deleted/$DAY/$name" "$@" "$src" "$dst" >> "$LOG" 2>&1; then
    say "$name ok"
  else
    ok=0; notes+=("$name rsync exit $?"); say "$name FAILED"
  fi
}

# 2. covers and the docker setup (the page cache rebuilds itself; never copy it)
mirror covers "$COVERS" "$DEST/covers/"
mirror docker "$DOCKER" "$DEST/docker/" --exclude "apps/kometa/data/page-cache/" --exclude "apps/kometa/data/covers/" \
  --exclude "*.db-wal" --exclude "*.db-shm" --exclude ".DS_Store"

# 3. the comics, from the NAS — the big one; incremental after the first night
if [ "${COMICS:-1}" = "1" ]; then
  if [ -d "$COMICS_SRC" ] && [ -n "$(ls -A "$COMICS_SRC" 2>/dev/null | head -1)" ]; then
    mirror comics "$COMICS_SRC" "$DEST/comics/" --exclude "@eaDir/" --exclude "#recycle/" --exclude ".DS_Store"
  else
    ok=0; notes+=("comics source missing or empty: $COMICS_SRC"); say "comics SKIPPED: source missing/empty"
  fi
fi

# 4. deleted/replaced files older than the window go for good
find "$DEST/deleted" -mindepth 1 -maxdepth 1 -type d -mtime +$KEEP_DELETED_DAYS -exec rm -rf {} + 2>/dev/null
rmdir "$DEST/deleted/$DAY" 2>/dev/null   # nothing removed tonight: no empty folder

size=$(du -sh "$DEST" 2>/dev/null | cut -f1)
printf '{"at":"%s","ok":%s,"size":"%s","notes":"%s"}\n' "$(date '+%F %T')" "$([ $ok = 1 ] && echo true || echo false)" "$size" "${notes[*]:-}" > "$DEST/status.json"
say "--- done ok=$ok size=$size ${notes[*]:-}"
[ $ok = 1 ]
