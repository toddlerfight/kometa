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


class TrashError(RuntimeError):
    pass


def _inside(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def trash_series(series_id: int, root: str | None = None) -> dict:
    """ONE Remove, one meaning (agreed 2026-10-08): the series goes, and so do its
    files — folder to _trash, purged after TRASH_DAYS, Undo meanwhile. 'Untrack'
    is not a thing any more: a folder left on disk would only be re-imported by
    the next shelf scan. Pull list OFF is how you stop fetching something.

    A series with no folder on disk (an arc, a run you tracked and never got)
    has nothing to bin — it's simply forgotten."""
    root = root or sources.comics_root()
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise TrashError("No such series")
    folder = s.get("folder_path")
    if folder and not os.path.isdir(folder):
        # folder_path is set but the folder isn't readable — genuinely gone, or the share
        # dropped. If the comics root itself can't be read, the mount is down: refuse,
        # rather than delete the DB row and strand the files (they'd come back as an
        # untracked series on remount).
        try:
            root_live = os.path.isdir(root) and any(os.scandir(root))
        except OSError:
            root_live = False
        if not root_live:
            raise TrashError("Comics share looks down — can't read the folder to bin it. Try again once the mount is back.")
    if not folder or not os.path.isdir(folder):
        db.dequeue_waiting_series(series_id, DB_PATH)
        db.remove_series(series_id, DB_PATH)
        logger.info(f"Removed {s['title']!r} (no folder on disk — nothing to bin)")
        return {"title": s["title"], "trashed_to": None, "purge_days": TRASH_DAYS}
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


def _marked_below(d: str) -> bool:
    return any(ORIGIN_FILE in files for _, _, files in os.walk(d))


def _units(d: str, depth: int = 1):
    """Each trashed thing once, and never a folder inside one: the first folder down any
    path that carries an origin marker, however deep it sits. The purge used to assume
    _trash/<publisher>/<series>/: a series trashed from straight under the root is
    _trash/<series>/, so its own subfolders were taken for trashed series — unmarked,
    aged by their own (old) mtime, deleted on the next purge with no week of Undo
    (2026-10-17). A folder with no marker anywhere in it (the retag job's dated folder,
    two deep) is still a unit at depth 2, as before."""
    for e in list(os.scandir(d)):
        if not e.is_dir(follow_symlinks=False):
            continue
        if os.path.exists(os.path.join(e.path, ORIGIN_FILE)):
            yield e.path
        elif depth >= 2 and not _marked_below(e.path):
            yield e.path
        else:
            yield from _units(e.path, depth + 1)


def purge(root: str | None = None, days: int = TRASH_DAYS, now: float | None = None) -> int:
    """Delete trashed folders older than `days` (by the time they were trashed —
    the origin file's mtime). Returns how many went. Emptied folders follow."""
    root = root or sources.comics_root()
    trash_root = os.path.join(root, TRASH_DIR)
    if not os.path.isdir(trash_root):
        return 0
    now = now or time.time()
    gone = 0
    for unit in list(_units(trash_root)):
        stamp = os.path.join(unit, ORIGIN_FILE)
        try:
            when = os.stat(stamp).st_mtime if os.path.exists(stamp) else os.stat(unit).st_mtime
        except OSError:
            continue
        if now - when >= days * 86400:
            shutil.rmtree(unit, ignore_errors=True)
            gone += 1
            logger.info(f"Purged from trash: {unit}")
    for d, _, _ in sorted(os.walk(trash_root), key=lambda w: -w[0].count(os.sep)):
        if d != trash_root:
            try:
                os.rmdir(d)             # only succeeds when empty — that's the point
            except OSError:
                pass
    return gone


def purge_safe():
    try:
        purge()
    except Exception as e:
        logger.warning(f"Trash purge failed: {e}")
