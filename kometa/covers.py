"""Covers and facts from the files we already hold — no network, no Komga.

Komga retirement, step 1 (2026-10-10). Komga's one real job left was being
first in the cover chain, and it made its thumbnails from the same page 1 we
can read ourselves. So: for every owned book, page 1 at cover size goes into
the image store under its own key (`issue/<sid>/<n>/file`, `book/<id>` for an
untracked shelf book, `series/<sid>/file` for the run), and the ComicInfo.xml
riding inside the archive goes into the issue record where no catalogue has
answered yet. A page 1 that stops halfway (reader.TruncatedCover) is skipped
and remembered, so the catalogue's cover wins for that issue and the tick
doesn't retry it every five minutes.

The cover routes read this store before anything that needs a network.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import kometa.db as db
from kometa import images

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
TRICKLE_LIMIT = 150
FAIL_RETRY_DAYS = 7
_ROLES = (("Writer", "writer"), ("Penciller", "penciller"), ("Inker", "inker"), ("Colorist", "colorist"),
          ("Letterer", "letterer"), ("CoverArtist", "cover"), ("Editor", "editor"))


# --- keys -----------------------------------------------------------------------
def file_key(series_id: int, number: float) -> str:
    return f"issue/{int(series_id)}/{number:g}/file"


def book_key(book_id: int) -> str:
    return f"book/{int(book_id)}"


def series_file_key(series_id: int) -> str:
    return f"series/{int(series_id)}/file"


def series_komga_key(series_id: int) -> str:
    return f"series/{int(series_id)}/komga"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- the store, for bytes we made rather than fetched -------------------------------
def store_bytes(key: str, data: bytes, source: str, path=None, root: str | None = None, note: str | None = None) -> dict | None:
    """Keep bytes under a key. `note` rides in the url column (an id, never a URL)."""
    path = path or DB_PATH
    images.ensure_tables(path)
    if not data:
        return None
    dest = images.file_for(key, root)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO image_record (key, path, url, bytes, fetched_at, source, failed_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)""", (key, dest, note, len(data), _now(), source))
    return images.get(key, path)


def mark_failed(key: str, source: str, path=None, note: str | None = None) -> None:
    path = path or DB_PATH
    images.ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("""INSERT INTO image_record (key, path, url, bytes, fetched_at, source, failed_at) VALUES (?, NULL, ?, 0, NULL, ?, ?)
            ON CONFLICT(key) DO UPDATE SET url = excluded.url, source = excluded.source, failed_at = excluded.failed_at""",
                     (key, note, source, _now()))


def local(key: str, path=None) -> str | None:
    """The file on disk for a key the store holds, else None."""
    return images.local_path(key, path)


# --- page 1 of a file, into the store ----------------------------------------------
def cover_bytes_for_file(key: str, file_path: str, path=None, root: str | None = None, note: str | None = None) -> bytes | None:
    """Generate the cover of one archive, keep it under `key`, and return the
    bytes. None when page 1 is cut short or unreadable — remembered as failed
    so it isn't retried every tick. A store that can't be written to (a
    read-only data dir) still serves: the bytes come back, only unkept."""
    from kometa import reader
    try:
        data = reader.get_cover_bytes(file_path)
    except reader.TruncatedCover:
        mark_failed(key, "file", path, note="truncated")
        return None
    except Exception as e:
        logger.info(f"Cover from file skipped for {os.path.basename(file_path)!r}: {e}")
        mark_failed(key, "file", path, note=str(e)[:80])
        return None
    try:
        store_bytes(key, data, "file", path, root, note=note)
    except OSError as e:
        logger.info(f"Cover store not writable for {key}: {e}")
    return data


def cover_for_file(key: str, file_path: str, path=None, root: str | None = None, note: str | None = None) -> bool:
    return cover_bytes_for_file(key, file_path, path, root, note) is not None


def _series_file_cover(series_id: int, number: float, path=None, root: str | None = None) -> None:
    """The run's own card cover is its lowest-numbered owned issue's page 1."""
    cur = images.get(series_file_key(series_id), path)
    cur_n = None
    if cur and cur.get("path") and (cur.get("url") or "").startswith("issue:"):
        try:
            cur_n = float(cur["url"][6:])
        except ValueError:
            cur_n = None
    if cur and cur.get("path") and cur_n is not None and cur_n <= number:
        return
    src = local(file_key(series_id, number), path)
    if not src:
        return
    try:
        data = open(src, "rb").read()
    except OSError:
        return
    store_bytes(series_file_key(series_id), data, "file", path, root, note=f"issue:{number:g}")


def generate_for_book(book: dict, path=None, root: str | None = None) -> str | None:
    """A books row → its cover in the store. Returns the key stored, or None."""
    path = path or DB_PATH
    sid, n = book.get("tracked_series_id"), book.get("number")
    if sid and n is not None:
        key = file_key(sid, n)
        if cover_for_file(key, book["path"], path, root, note=f"book:{book['id']}"):
            _series_file_cover(sid, float(n), path, root)
            return key
        return None
    key = book_key(book["id"])
    return key if cover_for_file(key, book["path"], path, root) else None


def generate_for_path(file_path: str, series_id: int | None = None, path=None, root: str | None = None) -> str | None:
    """Right after a download lands: the cover now, before any scan or tick.
    Keyed by series + number parsed from the file name; an untracked or
    unparseable file waits for the shelf scan and the trickle."""
    path = path or DB_PATH
    if not series_id:
        return None
    s = db.get_series_by_id(series_id, path)
    if not s:
        return None
    from kometa.naming import parse_issue_number
    n = parse_issue_number(os.path.basename(file_path), s.get("title") or "")
    if n is None:
        return None
    key = file_key(series_id, n)
    if cover_for_file(key, file_path, path, root):
        _series_file_cover(series_id, float(n), path, root)
        return key
    return None


# --- ComicInfo.xml, into the issue record ------------------------------------------
def read_comicinfo(file_path: str) -> dict | None:
    """The ComicInfo.xml inside a CBZ/CBR, as a flat dict of its tags, or None."""
    import zipfile
    data = None
    try:
        with open(file_path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return None
    if magic[:2] == b"PK":
        try:
            with zipfile.ZipFile(file_path) as zf:
                name = next((n for n in zf.namelist() if n.lower().rsplit("/", 1)[-1] == "comicinfo.xml"), None)
                if name:
                    data = zf.read(name)
        except Exception:
            return None
    elif magic == b"Rar!":
        try:
            from kometa import reader
            st = os.stat(file_path)
            d = reader._extract_rar(file_path, reader._version(st.st_size, st.st_mtime))
            for r, _, fs in os.walk(d):
                for f in fs:
                    if f.lower() == "comicinfo.xml":
                        data = open(os.path.join(r, f), "rb").read()
                        break
                if data:
                    break
        except Exception:
            return None
    if not data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    out = {}
    for el in root:
        tag = el.tag.split("}")[-1]
        if el.text and el.text.strip():
            out[tag] = el.text.strip()
    return out or None


def comicinfo_to_record(ci: dict) -> dict:
    """ComicInfo tags → the issue_meta shape write_issue takes."""
    credits = []
    for tag, role in _ROLES:
        for name in re.split(r"\s*,\s*", ci.get(tag) or ""):
            if name:
                credits.append({"role": role, "name": name, "metron_creator_id": None})
    y, m, d = ci.get("Year"), ci.get("Month"), ci.get("Day")
    store_date = None
    if y and y.isdigit():
        try:
            store_date = f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
        except ValueError:
            store_date = None
    pages = ci.get("PageCount")
    return {"desc": ci.get("Summary") or "", "credits": credits, "arcs": [a for a in [ci.get("StoryArc")] if a],
            "covers": [], "store_date": store_date, "cover_date": None,
            "page_count": int(pages) if pages and pages.isdigit() else None,
            "price": None, "isbn": None, "title": ci.get("Title"), "publisher": ci.get("Publisher"),
            "web": ci.get("Web"), "notes": ci.get("Notes")}


def _record_is_open(series_id: int, number: float, path) -> bool:
    """True when the record has nothing better than a miss for this issue —
    a catalogue row (metron/locg) is never overwritten by a file's own tags."""
    from kometa import record
    row = record.get_issue(series_id, number, path)
    return row is None or row.get("fill_state") == "miss" or row.get("source") in (None, "", "comicinfo", "komga")


def harvest_comicinfo(book: dict, path=None, issue: dict | None = None) -> bool:
    """One book's ComicInfo.xml into the record if nothing better is there."""
    path = path or DB_PATH
    sid, n = book.get("tracked_series_id"), book.get("number")
    if not sid or n is None:
        return False
    if not _record_is_open(sid, float(n), path):
        return False
    ci = read_comicinfo(book["path"])
    if not ci:
        return False
    from kometa import record
    if issue is None:
        issue = next((i for i in db.get_issues_for_series(sid, path) if i["number"] == float(n)), None) or {}
    record.write_issue(sid, float(n), comicinfo_to_record(ci), "comicinfo", "partial", issue, path)
    return True


# --- the trickle ------------------------------------------------------------------
def pending_books(path=None, limit: int = TRICKLE_LIMIT) -> list[dict]:
    """Owned books the store has no file cover for (and no fresh failure).
    Pull-list series first, then the newest files."""
    path = path or DB_PATH
    images.ensure_tables(path)
    with db._connect(path) as conn:
        have = {r[0] for r in conn.execute(
            "SELECT key FROM image_record WHERE (key LIKE 'issue/%/file' OR key LIKE 'book/%') AND "
            "(path IS NOT NULL OR failed_at > datetime('now', ?))", (f"-{FAIL_RETRY_DAYS} days",))}
        rows = [dict(r) for r in conn.execute("""
            SELECT b.id, b.path, b.tracked_series_id, b.number, b.added_at
            FROM books b LEFT JOIN tracked_series s ON s.id = b.tracked_series_id
            ORDER BY COALESCE(s.on_pull_list, 0) DESC, b.added_at DESC, b.id DESC""")]
    out = []
    for b in rows:
        key = file_key(b["tracked_series_id"], b["number"]) if b.get("tracked_series_id") and b.get("number") is not None else book_key(b["id"])
        if key in have:
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def covers_trickle(limit: int = TRICKLE_LIMIT, path=None, root: str | None = None) -> dict:
    """Scheduler tick: page 1 and ComicInfo for up to `limit` owned books.
    Disk and CPU only — there is no rate limit to respect, but a tick stays
    bounded so it never sits on a sync's shoulders for long."""
    path = path or DB_PATH
    done, failed, facts = 0, 0, 0
    issues_cache: dict[int, dict[float, dict]] = {}
    for b in pending_books(path, limit):
        if not os.path.exists(b["path"]):
            continue
        key = generate_for_book(b, path, root)
        if key:
            done += 1
        else:
            failed += 1
        try:
            sid, n = b.get("tracked_series_id"), b.get("number")
            if sid and n is not None:
                if sid not in issues_cache:
                    issues_cache[sid] = {i["number"]: i for i in db.get_issues_for_series(sid, path)}
                if harvest_comicinfo(b, path, issue=issues_cache[sid].get(float(n), {})):
                    facts += 1
        except Exception as e:
            logger.info(f"ComicInfo for book {b['id']} skipped: {e}")
    if done or failed or facts:
        logger.info(f"Covers trickle: {done} file cover(s) made, {failed} skipped, {facts} ComicInfo row(s) added")
    return {"covers": done, "skipped": failed, "comicinfo": facts}


def backlog(path=None) -> dict:
    path = path or DB_PATH
    images.ensure_tables(path)
    with db._connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        have = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM image_record WHERE source = 'file' AND path IS NOT NULL "
                            "AND (key LIKE 'issue/%/file' OR key LIKE 'book/%')").fetchone()
        failed = conn.execute("SELECT COUNT(*) FROM image_record WHERE source = 'file' AND path IS NULL").fetchone()[0]
    return {"books": total, "file_covers": have[0], "bytes": have[1], "failed": failed}
