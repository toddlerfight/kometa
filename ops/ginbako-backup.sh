#!/bin/bash
# Nightly backup of everything Kometa needs, onto the Voot II drive plugged into
# ginbako. Run by launchd (ops/dev.kometa.backup.plist) at 02:00 — before Kometa's
# own 03:30 copy to the NAS (the first full comics copy may run into it; later nights take minutes).
#
#   Voot II/Kometa-Backup/
#     db/kometa-YYYYmmdd-HHMM.db   a consistent copy of the live DB (made inside the container), 14 kept
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
COVERS="/Volumes/Catbus/kometa/covers/"           # on Catbus since 2026-10-13 (bind-mounted to /data/covers)
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

# 1. database: an online-safe copy, then prove it opens and is whole.
# The copy is made INSIDE the container. macOS sqlite opening the live file through
# OrbStack's share fights the container's WAL locks — 2026-10-13 a host-side .backup
# left every container read failing with "disk I/O error" until a stop + fresh -shm.
# The host only ever touches the finished copy.
TMPDB="$HOME/docker/apps/kometa/data/.voot-backup.db"
rm -f "$TMPDB"
if /usr/local/bin/docker exec kometa python -c "
import sqlite3
src = sqlite3.connect('/data/kometa.db'); dst = sqlite3.connect('/data/.voot-backup.db')
src.backup(dst); dst.close(); src.close()" >> "$LOG" 2>&1 \
   && mv "$TMPDB" "$DEST/db/kometa-$STAMP.db"; then
  chk=$(/usr/bin/sqlite3 "$DEST/db/kometa-$STAMP.db" "PRAGMA integrity_check;" 2>&1 | head -1)
  books=$(/usr/bin/sqlite3 "$DEST/db/kometa-$STAMP.db" "SELECT COUNT(*) FROM books;" 2>&1)
  if [ "$chk" = "ok" ]; then say "db ok ($books books)"; else ok=0; notes+=("db integrity: $chk"); say "db FAILED integrity: $chk"; fi
  rm -f "$DEST/db/kometa-$STAMP.db-wal" "$DEST/db/kometa-$STAMP.db-shm"   # the check opens it: leave the copy, not its scratch files
  ls -1t "$DEST"/db/kometa-*.db 2>/dev/null | tail -n +$((KEEP_DB + 1)) | while read -r f; do rm -f "$f"; done
else
  ok=0; notes+=("db copy failed"); say "db copy FAILED"
  rm -f "$TMPDB"
fi

mirror() {   # name src dst [extra rsync args...]
  local name=$1 src=$2 dst=$3; shift 3
  local rc=0
  /usr/bin/rsync -a --delete --backup --backup-dir="$DEST/deleted/$DAY/$name" "$@" "$src" "$dst" >> "$LOG" 2>&1 || rc=$?
  if [ $rc = 0 ]; then
    say "$name ok"
  else   # read rc, not $? — by the time the note was built, ok=0 had already reset it to 0
    ok=0; notes+=("$name rsync exit $rc"); say "$name FAILED (rsync exit $rc)"
  fi
}

# 2. covers and the docker setup (the page cache rebuilds itself; never copy it)
# Catbus unplugged = skip covers, loudly. Never mirror a missing or empty source with --delete.
if mount | grep -q " on /Volumes/Catbus " && [ -n "$(ls -A "$COVERS" 2>/dev/null)" ]; then
  mirror covers "$COVERS" "$DEST/covers/"
else
  ok=0; notes+=("covers skipped: Catbus not mounted or covers empty"); say "covers SKIPPED (Catbus not mounted / empty)"
fi
mirror docker "$DOCKER" "$DEST/docker/" --exclude "apps/kometa/data/page-cache/" --exclude "apps/kometa/data/covers/" \
  --exclude "apps/kometa/data/covers.moved-*/" \
  --exclude "*.db-wal" --exclude "*.db-shm" --exclude ".voot-backup.db" --exclude ".DS_Store"

# 3. the comics, from the NAS — the big one; incremental after the first night.
# Paused on purpose during the full ComicInfo rewrite (INSTRUCTIONS.md, "batch run"):
# the mirror must keep the ORIGINALS until the user has seen the new files, and
# mustn't chew the NAS while the swap does. Deliberate, so not a failure: ok stays 1.
PAUSE_COMICS="$HOME/docker/apps/kometa/data/.pause-comics-backup"
if [ "${COMICS:-1}" = "1" ] && [ -e "$PAUSE_COMICS" ]; then
  notes+=("comics paused (retag batch)"); say "comics PAUSED (retag batch)"
elif [ "${COMICS:-1}" = "1" ]; then
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
