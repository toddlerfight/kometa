"""Remove a series you've decided is trash — from Kometa AND from disk.

For the Needs matching list (docs/reader-spec.md): an unmatched folder is often
a mis-named rip, a duplicate, or something you never wanted. "Remove" drops the
series, its shelf entry, books and read progress, and MOVES the folder to
`_trash/` under the comics root. The shelf scan skips underscore folders, so it
is gone from every view at once — but the files sit there for TRASH_DAYS before
the purge deletes them for real, and Undo puts them straight back. Deleting is
forever; moving is a week of second thoughts.
"""
import logging
import os
import shutil
import time

import kometa.db as db
from kometa import sources

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH
TRASH_DIR = "_trash"
TRASH_DAYS = 7
ORIGIN_FILE = ".kometa-origin"     # inside a trashed folder: where it came from
REMOVABLE = {"pending", "needs_match"}


class TrashError(RuntimeError):
    pass


def _inside(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def trash_series(series_id: int, root: str | None = None) -> dict:
    """Move the folder to _trash and forget the series. Only for series that
    aren't matched — a matched run is a library item, not a candidate for the bin."""
    root = root or sources.comics_root()
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise TrashError("No such series")
    if s.get("match_status") not in REMOVABLE:
        raise TrashError("Only unmatched series can be removed this way")
    folder = s.get("folder_path")
    if not folder or not os.path.isdir(folder):
        raise TrashError("The series has no folder on disk")
    if not _inside(folder, root) or os.path.realpath(folder) == os.path.realpath(root):
        raise TrashError("Folder is outside the comics root")
    trash_root = os.path.join(root, TRASH_DIR)
    dest = os.path.join(trash_root, os.path.relpath(folder, root))
    if os.path.exists(dest):
        dest = f"{dest} ({int(time.time())})"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(folder, dest)
    with open(os.path.join(dest, ORIGIN_FILE), "w") as f:
        f.write(folder)
    # the folder is gone: so is everything that pointed at it
    db.dequeue_waiting_series(series_id, DB_PATH)
    db.remove_books_under(folder, DB_PATH)
    db.remove_shelf_series_by_path(folder, DB_PATH)
    db.remove_series(series_id, DB_PATH)
    logger.info(f"Trashed {s['title']!r}: {folder} -> {dest} (purge in {TRASH_DAYS}d)")
    return {"title": s["title"], "trashed_to": dest, "purge_days": TRASH_DAYS}


def restore(trashed_path: str, root: str | None = None) -> str:
    """Undo: put a trashed folder back where it was. Returns the restored path.
    The caller rescans the shelf so it becomes a series again."""
    root = root or sources.comics_root()
    trash_root = os.path.join(root, TRASH_DIR)
    if not _inside(trashed_path, trash_root) or not os.path.isdir(trashed_path):
        raise TrashError("Not a trashed folder")
    origin_file = os.path.join(trashed_path, ORIGIN_FILE)
    try:
        with open(origin_file) as f:
            origin = f.read().strip()
    except OSError:
        raise TrashError("This folder doesn't remember where it came from")
    if not _inside(origin, root) or os.path.exists(origin):
        raise TrashError("Its original place is taken or outside the comics root")
    os.remove(origin_file)
    os.makedirs(os.path.dirname(origin), exist_ok=True)
    shutil.move(trashed_path, origin)
    logger.info(f"Restored {origin} from trash")
    return origin


def purge(root: str | None = None, days: int = TRASH_DAYS, now: float | None = None) -> int:
    """Delete trashed folders older than `days` (by the time they were trashed —
    the origin file's mtime). Returns how many went. Empty publisher dirs follow."""
    root = root or sources.comics_root()
    trash_root = os.path.join(root, TRASH_DIR)
    if not os.path.isdir(trash_root):
        return 0
    now = now or time.time()
    gone = 0
    for pub in list(os.scandir(trash_root)):
        if not pub.is_dir():
            continue
        for ser in list(os.scandir(pub.path)):
            if not ser.is_dir():
                continue
            stamp = os.path.join(ser.path, ORIGIN_FILE)
            try:
                when = os.stat(stamp).st_mtime if os.path.exists(stamp) else ser.stat().st_mtime
            except OSError:
                continue
            if now - when >= days * 86400:
                shutil.rmtree(ser.path, ignore_errors=True)
                gone += 1
                logger.info(f"Purged from trash: {ser.path}")
        try:
            os.rmdir(pub.path)          # only succeeds when empty — that's the point
        except OSError:
            pass
    return gone


def purge_safe():
    try:
        purge()
    except Exception as e:
        logger.warning(f"Trash purge failed: {e}")
