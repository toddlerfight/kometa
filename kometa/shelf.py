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
from kometa.auth import current_reader
from kometa.naming import PIPELINE_EXTS
from kometa import sources
from kometa.naming import parse_issue_number
from kometa import reader as rd

logger = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = db.DB_PATH

# What the reader can open = what the pipeline can open (naming.PIPELINE_EXTS):
# one definition, not a third copy. PDF / CB7 / CBT count as OWNED (naming.
# OWNED_EXTS) but the reader can't page them, so they'd only be dead tiles.
# 2026-10-10: zero such files on the shelf; if one lands, convert it to CBZ.
READABLE_EXTS = PIPELINE_EXTS

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


def _series_folders(ser) -> list[tuple[str, str, list]]:
    """(folder, title, files) for a series dir: itself, plus any sub-folder
    holding comics. 'Batman (2016)' keeps its name (it says which run it is);
    'Volume 07 (2016)' or 'Variants' becomes 'Batman Beyond - Volume 07 (2016)'.
    Each folder is listed once — its files ride along (the scan used to list
    every sub-folder twice: once to see if it held comics, once to index them)."""
    out = [(ser.path, ser.name, _comic_files(ser.path))]
    try:
        subs = [d for d in os.scandir(ser.path) if d.is_dir() and not _skip(d.name)]
    except OSError:
        return out
    parent = ser.name.lower()
    for d in sorted(subs, key=lambda e: e.name.lower()):
        files = _comic_files(d.path)
        if not files:
            continue
        stands_alone = d.name.lower().startswith(parent)
        out.append((d.path, d.name if stands_alone else f"{ser.name} - {d.name}", files))
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
        tracked = {db.nfc(os.path.realpath(s["folder_path"])): s["id"]
                   for s in db.get_all_series(DB_PATH)
                   if s.get("folder_path") and s.get("kind") != "arc"}
        # microseconds: the prune keys on "not stamped by THIS scan", so two scans
        # inside one second must not share a stamp
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        n_series = n_books = gone = 0
        for pub in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if not pub.is_dir() or _skip(pub.name):
                continue
            # list the publisher first (the share), THEN write it in one transaction
            # (the DB) — ~1,600 separate commits a scan before, and never a write
            # lock held while the share is being read
            found = []
            for ser in os.scandir(pub.path):
                if not ser.is_dir() or _skip(ser.name):
                    continue
                # Publisher/Series/<files> — and one level deeper, because the
                # shelf has Batman/Batman (2016)/, Batman Beyond/Volume 07 (2016)/,
                # Deadpool/Variants/: 345 files the two-level walk never saw
                # (2026-10-08, found by the Komga history import's unmatched list).
                # A sub-folder is its own series, titled by its own name when that
                # name stands alone ('Batman (2016)'), else 'Parent - Sub'.
                found += [f for f in _series_folders(ser) if f[2]]
            with db._connect(DB_PATH):
                for folder, title, files in found:
                    tid = tracked.get(db.nfc(os.path.realpath(folder)))
                    sid = db.upsert_shelf_series(folder, title, pub.name, tid, len(files), stamp, DB_PATH)
                    db.index_books([(p, size, mtime, parse_issue_number(name, title), sid, tid)
                                    for p, size, mtime, name in files], DB_PATH)
                    gone += db.forget_missing_books(sid, [f[0] for f in files], DB_PATH)
                    n_series += 1
                    n_books += len(files)
        # a folder the walk didn't see AND that's really not there: its unread books
        # go too (read ones keep their history; one that merely failed to list keeps all)
        with db._connect(DB_PATH) as conn:
            vanished = [(r["id"], r["folder_path"]) for r in conn.execute(
                "SELECT id, folder_path FROM shelf_series WHERE scanned_at IS NOT ?", (stamp,))]
        for vid, vfolder in vanished:
            if not os.path.isdir(vfolder):
                gone += db.forget_missing_books(vid, [], DB_PATH, keep_read=True)
        pruned = db.prune_shelf(stamp, DB_PATH)
        # rows already cut loose from any folder (pruned before books were) whose
        # file is really gone and that nobody read: nothing points at them
        with db._connect(DB_PATH) as conn:
            loose = [(r["id"], r["path"]) for r in conn.execute("""
                SELECT b.id, b.path FROM books b LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
                WHERE s.id IS NULL AND NOT EXISTS (SELECT 1 FROM read_progress p WHERE p.book_id = b.id)""")]
        # …or that sits in a folder the shelf skips on purpose (_misgrabbed, .parked)
        quarantined = lambda fp: any(_skip(part) for part in os.path.relpath(fp, root).split(os.sep)[:-1])
        dead = [i for i, fp in loose if quarantined(fp) or not os.path.exists(fp)]
        if dead:
            with db._connect(DB_PATH) as conn:
                db._forget_book_ids(conn, dead)
            gone += len(dead)
        healed = db.reparse_null_numbers(DB_PATH)      # names that parse now (renamed, re-titled)
        if healed:
            logger.info(f"Shelf scan: {healed} book(s) given the number their name now carries")
        result = {"series": n_series, "books": n_books, "pruned": pruned, "books_gone": gone, "scanned_at": stamp}
        db.set_config({SCANNED_KEY: stamp}, DB_PATH)
        _last_scan.clear()
        _last_scan.update(result)
        logger.info(f"Shelf scan: {n_series} series, {n_books} books, {pruned} folders and {gone} files gone")
        return result
    finally:
        _scan_lock.release()


def index_series_folder(series_id: int, path=None) -> int:
    """One series' folder into the shelf, the moment a file lands in it — the
    whole-shelf walk is for the clock. Adds and refreshes; never prunes (that
    needs the full walk's stamp). Returns how many files the folder holds."""
    path = path or DB_PATH
    s = db.get_series_by_id(series_id, path)
    folder = (s or {}).get("folder_path")
    if not folder or not os.path.isdir(folder):
        return 0
    files = _comic_files(folder)
    shelf_id = db.shelf_id_for_series(series_id, path)
    if shelf_id is None:
        if not files:
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        shelf_id = db.upsert_shelf_series(folder, s["title"], os.path.basename(os.path.dirname(folder)), series_id,
                                          len(files), stamp, path)
    db.index_books([(fp, size, mtime, parse_issue_number(name, s["title"]), shelf_id, series_id)
                    for fp, size, mtime, name in files], path)
    db.forget_missing_books(shelf_id, [f[0] for f in files], path)
    return len(files)


SCANNED_KEY = "shelf_scanned_at"
STARTUP_FRESH_S = 6 * 3600


def _scanned_recently(within_s: float) -> bool:
    at = db.get_config(DB_PATH).get(SCANNED_KEY) or ""
    try:
        t = datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - t).total_seconds() < within_s


def scan_shelf_safe(skip_if_fresh: bool = False):
    # A restart doesn't walk the whole shelf again if a walk finished lately:
    # deploys came in bursts, each re-listing every folder and stat'ing ~7k files.
    # Downloads index their own folder as they land; every full sync still walks.
    if skip_if_fresh and _scanned_recently(STARTUP_FRESH_S):
        logger.info("Shelf scan: skipped at startup — the last one is under 6 hours old")
        _last_scan.setdefault("scanned_at", db.get_config(DB_PATH).get(SCANNED_KEY))
    else:
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


def scan_in_background(skip_if_fresh: bool = False):
    threading.Thread(target=scan_shelf_safe, kwargs={"skip_if_fresh": skip_if_fresh},
                     name="shelf-scan", daemon=True).start()


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
        "series": db.list_shelf(current_reader(), untracked_only=True, path=DB_PATH),
        "tracked_reading": db.reading_by_tracked_series(current_reader(), DB_PATH),
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
    # files the Trades tab already shows as owned editions: the series page keeps
    # them off 'Also on the shelf' so a trade isn't listed twice
    trade_files = set()
    if s.get("tracked_series_id"):
        cached = db.get_trades(s["tracked_series_id"], DB_PATH)
        trade_files = {t["file"] for t in (cached or {}).get("trades", []) if t.get("file")}
    # the index is the truth here: the shelf scan prunes rows whose file went,
    # and a file that vanished since says so when it's opened. A check per row
    # was 544 round trips to the share for one Uncanny X-Men page view.
    books = []
    for b in _sorted_books(db.shelf_books(shelf_id, current_reader(), DB_PATH), s["title"]):
        books.append({
            "id": b["id"],
            "number": b["number"],
            "trade": os.path.basename(b["path"]) in trade_files,
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
    # the first one still on disk — asked lazily, so it's one check, not one per book
    return next((b for b in _sorted_books(db.shelf_books(shelf_id, current_reader(), DB_PATH), s["title"])
                 if os.path.exists(b["path"])), None)


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
    # Your chosen variant wins, the same precedence the series page's tile has.
    if b.get("tracked_series_id") and b.get("number") is not None:
        url = db.variant_cover_url(b["tracked_series_id"], b["number"], DB_PATH)
        if url:
            from kometa.thumbnails import _image_or_none
            resp = _image_or_none(url)
            if resp:
                return resp
    try:
        return _cover_response(b["path"])
    except rd.TruncatedCover:
        # page 1 is cut short: the catalogue's cover for this issue instead
        if b.get("tracked_series_id") and b.get("number") is not None:
            from kometa.thumbnails import issue_thumbnail
            return issue_thumbnail(b["tracked_series_id"], b["number"])
        raise HTTPException(422, "Cover can't be rendered")


def _cover_response(path: str):
    try:
        data = rd.get_cover_bytes(path)
    except FileNotFoundError:
        raise HTTPException(404, "File is gone from the shelf")
    except rd.TruncatedCover:
        raise
    except Exception:
        raise HTTPException(422, "Cover can't be rendered")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})
