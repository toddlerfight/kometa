"""The whole shelf — every series folder under the comics root, tracked or not.

Kometa used to know only the ~100 series it tracks; the shelf holds ~900. To
read all of it (docs/reader-spec.md, step 2) the reader needs an index of every
book file. This is that index — and deliberately nothing more: untracked
folders never become tracked series, so sync, sweeps and LOCG never see them.

The scan only LISTS (names, sizes, mtimes). A book's pages are read the first
time it's opened, by the reader, same as before.
"""
import os
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

import kometa.db as db
from kometa import sources
from kometa.naming import parse_issue_number
from kometa import reader as rd

logger = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = db.DB_PATH

# What the reader can open. PDF / CB7 / CBT count as owned elsewhere but aren't
# readable here yet, so they'd only be dead tiles.
READABLE_EXTS = frozenset({".cbz", ".cbr", ".zip", ".rar"})

_scan_lock = threading.Lock()
_last_scan: dict = {}


def _skip(name: str) -> bool:
    # dot-dirs (.kometa-staging, quarantines), _underscore quarantines
    # (_misgrabbed), Synology's @eaDir / #recycle
    return name.startswith((".", "_", "@", "#"))


def _comic_files(folder: str) -> list[tuple]:
    out = []
    try:
        for f in os.scandir(folder):
            if f.is_file() and not _skip(f.name) and os.path.splitext(f.name)[1].lower() in READABLE_EXTS:
                st = f.stat()
                out.append((f.path, st.st_size, st.st_mtime, f.name))
    except OSError:
        pass
    return out


def _series_folders(ser) -> list[tuple[str, str]]:
    """(folder, title) for a series dir: itself, plus any sub-folder holding
    comics. 'Batman (2016)' keeps its name (it says which run it is); 'Volume
    07 (2016)' or 'Variants' becomes 'Batman Beyond - Volume 07 (2016)'."""
    out = [(ser.path, ser.name)]
    try:
        subs = [d for d in os.scandir(ser.path) if d.is_dir() and not _skip(d.name)]
    except OSError:
        return out
    parent = ser.name.lower()
    for d in sorted(subs, key=lambda e: e.name.lower()):
        if not _comic_files(d.path):
            continue
        stands_alone = d.name.lower().startswith(parent)
        out.append((d.path, d.name if stands_alone else f"{ser.name} - {d.name}"))
    return out


def scan_shelf(root: str | None = None) -> dict:
    """Walk root/Publisher/Series/<files>. Returns counts. One scan at a time —
    a second caller gets the running scan's last result instead of piling on."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running", **_last_scan}
    try:
        root = root or sources.comics_root()
        if not os.path.isdir(root) or not os.listdir(root):
            # an empty or missing root is a dead mount, not an empty library —
            # never prune the shelf on it
            raise RuntimeError(f"comics root {root!r} is missing or empty")
        tracked = {os.path.realpath(s["folder_path"]): s["id"]
                   for s in db.get_all_series(DB_PATH)
                   if s.get("folder_path") and s.get("kind") != "arc"}
        # microseconds: the prune keys on "not stamped by THIS scan", so two scans
        # inside one second must not share a stamp
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        n_series = n_books = 0
        for pub in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if not pub.is_dir() or _skip(pub.name):
                continue
            for ser in os.scandir(pub.path):
                if not ser.is_dir() or _skip(ser.name):
                    continue
                # Publisher/Series/<files> — and one level deeper, because the
                # shelf has Batman/Batman (2016)/, Batman Beyond/Volume 07 (2016)/,
                # Deadpool/Variants/: 345 files the two-level walk never saw
                # (2026-10-08, found by the Komga history import's unmatched list).
                # A sub-folder is its own series, titled by its own name when that
                # name stands alone ('Batman (2016)'), else 'Parent - Sub'.
                for folder, title in _series_folders(ser):
                    files = _comic_files(folder)
                    if not files:
                        continue
                    tid = tracked.get(os.path.realpath(folder))
                    sid = db.upsert_shelf_series(folder, title, pub.name, tid, len(files), stamp, DB_PATH)
                    db.index_books([(p, size, mtime, parse_issue_number(name, title), sid, tid)
                                    for p, size, mtime, name in files], DB_PATH)
                    n_series += 1
                    n_books += len(files)
        pruned = db.prune_shelf(stamp, DB_PATH)
        result = {"series": n_series, "books": n_books, "pruned": pruned, "scanned_at": stamp}
        _last_scan.clear()
        _last_scan.update(result)
        logger.info(f"Shelf scan: {n_series} series, {n_books} books, {pruned} gone")
        return result
    finally:
        _scan_lock.release()


def scan_shelf_safe():
    try:
        scan_shelf()
    except Exception as e:
        logger.warning(f"Shelf scan failed: {e}")
        return
    # Every series is a Kometa series: new folders become series (pull list off)
    # and get matched to LOCG in the background, throttled.
    from kometa.shelf_import import import_in_background
    import_in_background()
    # Removed series wait in _trash for a week, then go for real (kometa/trash.py).
    # Startup + every full sync is often enough.
    from kometa.trash import purge_safe
    purge_safe()


def scan_in_background():
    threading.Thread(target=scan_shelf_safe, name="shelf-scan", daemon=True).start()


# --- routes ------------------------------------------------------------------------

def _sorted_books(books: list[dict], title: str) -> list[dict]:
    """Issue order where a number parses, then the rest (trades, oddities) by name."""
    return sorted(books, key=lambda b: (b["number"] is None, b["number"] or 0,
                                        rd._natural_key(os.path.basename(b["path"]))))


@router.get("/api/shelf")
def list_shelf():
    """Untracked series on the shelf (tracked ones are /api/series), plus the
    Reading flags for tracked series so the Library chip covers both."""
    return {
        "series": db.list_shelf(rd.READER_ID, untracked_only=True, path=DB_PATH),
        "tracked_reading": db.reading_by_tracked_series(rd.READER_ID, DB_PATH),
        "last_scan": _last_scan or None,
    }


@router.post("/api/shelf/scan")
def rescan_shelf():
    scan_in_background()
    return {"ok": True}


@router.get("/api/shelf/{shelf_id}")
def shelf_detail(shelf_id: int):
    s = db.get_shelf_series(shelf_id, DB_PATH)
    if not s:
        raise HTTPException(404, "No such shelf series")
    books = []
    for b in _sorted_books(db.shelf_books(shelf_id, rd.READER_ID, DB_PATH), s["title"]):
        if not os.path.exists(b["path"]):
            continue
        books.append({
            "id": b["id"],
            "number": b["number"],
            "label": f"#{b['number']:g}" if b["number"] is not None else os.path.splitext(os.path.basename(b["path"]))[0],
            "page_count": b["page_count"],
            "progress": None if b["progress_page"] is None else
                {"page": b["progress_page"], "completed": b["completed"], "updated_at": b["updated_at"]},
        })
    return {**s, "books": books}


def _first_book(shelf_id: int) -> dict | None:
    s = db.get_shelf_series(shelf_id, DB_PATH)
    if not s:
        return None
    books = [b for b in _sorted_books(db.shelf_books(shelf_id, rd.READER_ID, DB_PATH), s["title"])
             if os.path.exists(b["path"])]
    return books[0] if books else None


@router.get("/api/shelf/{shelf_id}/cover")
def shelf_cover(shelf_id: int):
    """Page 1 of the first book. Kometa's own cover — no Komga needed."""
    first = _first_book(shelf_id)
    if not first:
        raise HTTPException(404, "No books")
    return _cover_response(first["path"])


@router.get("/api/books/{book_id}/cover")
def book_cover(book_id: int):
    b = db.get_book(book_id, DB_PATH)
    if not b:
        raise HTTPException(404, "No such book")
    return _cover_response(b["path"])


def _cover_response(path: str):
    try:
        data = rd.get_cover_bytes(path)
    except FileNotFoundError:
        raise HTTPException(404, "File is gone from the shelf")
    except Exception:
        raise HTTPException(422, "Cover can't be rendered")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})
