"""The reader's server half: books, pages, progress.

Komga used to be the only way to read anything Kometa fetched, and every handoff
to it — scan, series list, book list, read link — was a place for lag to hide.
This serves pages straight off the shelf instead.

Design rules (docs/reader-spec.md):
- Device-neutral: the web reader asks for 'page 7 of book 42 at 1440px', and a
  future native app asks exactly the same questions.
- Pages are resized server-side. Digital rips run ~10 MB a page; nobody's iPad
  needs that over a phone connection.
- Rendered pages live on LOCAL disk (next to the DB), not the NAS share — the
  share times out under heavy NAS I/O, and a cached page shouldn't care.
"""
import io
import os
import re
import shutil
import logging
import zipfile
import threading
import subprocess

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from PIL import Image

import kometa.db as db
from kometa.naming import find_issue_file, _IMAGE_EXTS

logger = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = db.DB_PATH

# One reader today. Everything is keyed by it so a second one is data, not schema.
READER_ID = "me"

# Finished = within the last N pages. Pages-from-end, not a percentage: backmatter
# (ads, letters, a variant cover) is a roughly fixed number of pages, and 90% is
# page 32 of a single issue but page 252 of an omnibus. The motivating case was an
# issue stuck 'in progress' at 35/36 for weeks.
FINISH_WITHIN_PAGES = 3

# Resize buckets. A client asks for its screen width; we serve the smallest bucket
# that covers it, so the cache holds a handful of sizes per page, not one per
# device. Never upscaled — a page smaller than the bucket is served as-is.
WIDTH_BUCKETS = (720, 1080, 1440, 2048)
DEFAULT_WIDTH = 1440
JPEG_QUALITY = 85

PAGE_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "page-cache")
PAGE_CACHE_MAX_BYTES = int(float(os.environ.get("KOMETA_PAGE_CACHE_GB", "20")) * 1024 ** 3)
_EVICT_EVERY = 200          # check the cache size every N fresh renders
_renders_since_evict = 0
_evict_lock = threading.Lock()


# --- archives ------------------------------------------------------------------

def _natural_key(name: str):
    """'p2' before 'p10'. Rip filenames are numbered but rarely zero-padded."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _is_page(name: str) -> bool:
    base = os.path.basename(name)
    return (name.lower().endswith(_IMAGE_EXTS)
            and not name.startswith("__MACOSX")
            and not base.startswith("."))


def _version(size: int, mtime: float) -> str:
    return f"{int(mtime)}-{size}"


def _rar_dir(path: str, version: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]", "_", os.path.basename(path))[:60]
    return os.path.join(PAGE_CACHE_DIR, "_rar", f"{key}-{version}")


_rar_locks: dict[str, threading.Lock] = {}
_rar_locks_guard = threading.Lock()


def _extract_rar(path: str, version: str) -> str:
    """CBRs can't be read page-at-a-time cheaply. Unpack once into the local
    cache and serve from there. Finalize converts new downloads to CBZ, so this
    only bites on older library files."""
    out = _rar_dir(path, version)
    if os.path.isdir(out) and os.listdir(out):
        return out
    with _rar_locks_guard:
        lock = _rar_locks.setdefault(out, threading.Lock())
    with lock:
        if os.path.isdir(out) and os.listdir(out):
            return out
        tmp = out + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        subprocess.run(["unar", "-q", "-f", "-D", "-o", tmp, path],
                       capture_output=True, timeout=300, check=False)
        os.replace(tmp, out)
    return out


class _Book:
    """Uniform read access over a CBZ or a CBR. Entry names are archive-relative."""

    def __init__(self, path: str, version: str):
        self.path = path
        with open(path, "rb") as fh:
            magic = fh.read(4)
        if magic[:2] == b"PK":
            self._zip = zipfile.ZipFile(path)
            self._dir = None
        elif magic == b"Rar!":
            self._zip = None
            self._dir = _extract_rar(path, version)
        else:
            raise ValueError("not a zip or rar archive")

    def names(self) -> list[str]:
        if self._zip:
            raw = self._zip.namelist()
        else:
            raw = [os.path.relpath(os.path.join(r, f), self._dir)
                   for r, _, fs in os.walk(self._dir) for f in fs]
        return sorted((n for n in raw if _is_page(n)), key=_natural_key)

    def open(self, name: str):
        if self._zip:
            return self._zip.open(name)
        full = os.path.realpath(os.path.join(self._dir, name))
        if not full.startswith(os.path.realpath(self._dir) + os.sep):
            raise ValueError("entry escapes the archive")
        return open(full, "rb")

    def close(self):
        if self._zip:
            self._zip.close()


def _scan_pages(path: str, version: str) -> list[list]:
    """[[entry_name, width, height], ...] in reading order. Dimensions come from
    the image header only (Pillow opens lazily) — needed up front so the reader
    can show a wide spread on its own instead of pairing it."""
    bk = _Book(path, version)
    try:
        pages = []
        for name in bk.names():
            w = h = 0
            try:
                with bk.open(name) as fh, Image.open(fh) as im:
                    w, h = im.size
            except Exception:
                pass                # unreadable header: still a page slot, size unknown
            pages.append([name, w, h])
        return pages
    finally:
        bk.close()


def ensure_book(path: str, tracked_series_id=None, number=None) -> dict:
    """Register a file as a book, or return the existing row if unchanged.
    A changed size/mtime (re-download, cover injection) rebuilds the page list."""
    try:
        st = os.stat(path)
    except OSError:
        raise HTTPException(404, "File is gone from the shelf")
    existing = db.get_book_by_path(path, DB_PATH)
    if existing and existing["size"] == st.st_size and existing["mtime"] == st.st_mtime and existing["pages"]:
        return existing
    try:
        pages = _scan_pages(path, _version(st.st_size, st.st_mtime))
    except Exception as e:
        logger.warning(f"Reader: can't open {path!r}: {e}")
        raise HTTPException(422, "This file can't be opened as a comic")
    if not pages:
        raise HTTPException(422, "This file has no pages")
    book_id = db.upsert_book(path, st.st_size, st.st_mtime, pages,
                             tracked_series_id=tracked_series_id, number=number, path=DB_PATH)
    return db.get_book(book_id, DB_PATH)


# --- page rendering + cache ---------------------------------------------------

def _bucket(w: int | None) -> int:
    if not w:
        return DEFAULT_WIDTH
    for b in WIDTH_BUCKETS:
        if w <= b:
            return b
    return WIDTH_BUCKETS[-1]


def _render(book: dict, index: int, width: int) -> bytes:
    name = book["pages"][index][0]
    bk = _Book(book["path"], _version(book["size"], book["mtime"]))
    try:
        with bk.open(name) as fh, Image.open(fh) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if im.width > width:
                im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return buf.getvalue()
    finally:
        bk.close()


def _page_cache_path(book: dict, index: int, width: int) -> str:
    return os.path.join(PAGE_CACHE_DIR, str(book["id"]),
                        _version(book["size"], book["mtime"]), f"{index + 1}_{width}.jpg")


def _evict_if_needed():
    """Oldest-touched books go first until the cache is under its cap. Book dirs
    are touched on every hit, so mtime is last-read time."""
    with _evict_lock:
        try:
            entries = []
            total = 0
            for d in os.scandir(PAGE_CACHE_DIR):
                if not d.is_dir():
                    continue
                size = sum(os.path.getsize(os.path.join(r, f))
                           for r, _, fs in os.walk(d.path) for f in fs)
                total += size
                entries.append((d.stat().st_mtime, d.path, size))
            for _, p, size in sorted(entries):
                if total <= PAGE_CACHE_MAX_BYTES:
                    break
                shutil.rmtree(p, ignore_errors=True)
                total -= size
        except Exception as e:
            logger.warning(f"Reader: page-cache eviction failed: {e}")


def get_page_bytes(book: dict, index: int, width: int) -> bytes:
    global _renders_since_evict
    cp = _page_cache_path(book, index, width)
    try:
        with open(cp, "rb") as fh:
            data = fh.read()
        try:
            os.utime(os.path.join(PAGE_CACHE_DIR, str(book["id"])))
        except OSError:
            pass
        return data
    except OSError:
        pass
    data = _render(book, index, width)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    tmp = cp + f".{threading.get_ident()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, cp)
    _renders_since_evict += 1
    if _renders_since_evict >= _EVICT_EVERY:
        _renders_since_evict = 0
        threading.Thread(target=_evict_if_needed, daemon=True).start()
    return data


# --- payloads -------------------------------------------------------------------

def is_finished(page: int, page_count: int) -> bool:
    return page_count > 0 and page >= page_count - (FINISH_WITHIN_PAGES - 1)


def _book_payload(book: dict) -> dict:
    title, publisher = None, None
    if book.get("tracked_series_id"):
        s = db.get_series_by_id(book["tracked_series_id"], DB_PATH)
        if s:
            title, publisher = s["title"], s.get("publisher")
    if not title:
        title = os.path.basename(os.path.dirname(book["path"]))
    return {
        "id": book["id"],
        "title": title,
        "publisher": publisher,
        "series_id": book.get("tracked_series_id"),
        "number": book.get("number"),
        "version": _version(book["size"], book["mtime"]),
        "page_count": book["page_count"],
        # wide = a two-page spread drawn as one image: shown on its own, never paired
        "pages": [{"w": w, "h": h, "wide": bool(w and h and w > h)} for _, w, h in book["pages"]],
        "finish_within_pages": FINISH_WITHIN_PAGES,
        "progress": db.get_progress(READER_ID, book["id"], DB_PATH),
    }


def _book_or_404(book_id: int) -> dict:
    book = db.get_book(book_id, DB_PATH)
    if not book:
        raise HTTPException(404, "No such book")
    return book


# --- routes ---------------------------------------------------------------------

@router.get("/api/series/{series_id}/issues/{number}/book")
def issue_book(series_id: int, number: float):
    """The book behind an owned issue — the 'Read' button's first hop."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404, "No such series")
    path = find_issue_file(s.get("folder_path"), s["title"], number)
    if not path:
        raise HTTPException(404, "No file on the shelf for this issue")
    return _book_payload(ensure_book(path, tracked_series_id=series_id, number=number))


@router.get("/api/books/{book_id}")
def book_detail(book_id: int):
    book = _book_or_404(book_id)
    book = ensure_book(book["path"], book.get("tracked_series_id"), book.get("number"))
    return _book_payload(book)


@router.get("/api/books/{book_id}/pages/{page}")
def book_page(book_id: int, page: int, w: int | None = None):
    """Page `page` (1-based), resized to the bucket covering `w`. The client
    appends ?v=<version> so a re-downloaded file busts the browser cache —
    which is why the response can be cached as immutable."""
    book = _book_or_404(book_id)
    if not 1 <= page <= (book["page_count"] or 0):
        raise HTTPException(404, "No such page")
    try:
        data = get_page_bytes(book, page - 1, _bucket(w))
    except FileNotFoundError:
        raise HTTPException(404, "File is gone from the shelf")
    except Exception as e:
        logger.warning(f"Reader: page {page} of book {book_id} failed: {e}")
        raise HTTPException(422, "This page can't be rendered")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


class ProgressRequest(BaseModel):
    page: int
    updated_at: str            # client clock, UTC ISO-8601 'YYYY-MM-DDTHH:MM:SS(.fff)Z'
    completed: bool | None = None


_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


@router.put("/api/books/{book_id}/progress")
def put_progress(book_id: int, req: ProgressRequest):
    """Explicit progress writes. A write older than what's stored is refused
    with 409 + the stored progress, so an offline device replaying a queue can
    never drag you backwards."""
    book = _book_or_404(book_id)
    if not 1 <= req.page <= (book["page_count"] or 0):
        raise HTTPException(422, "Page out of range")
    if not _ISO_UTC.match(req.updated_at):
        raise HTTPException(422, "updated_at must be UTC ISO-8601, e.g. 2026-10-08T09:30:00Z")
    completed = req.completed if req.completed is not None else is_finished(req.page, book["page_count"])
    ok, stored = db.set_progress(READER_ID, book_id, req.page, completed, req.updated_at, DB_PATH)
    if not ok:
        raise HTTPException(409, detail={"error": "stale", "progress": stored})
    return {"progress": stored}
