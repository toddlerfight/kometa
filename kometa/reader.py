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
from PIL import Image, ImageFile

# A page missing its last few bytes still has every pixel that matters. Pillow
# refuses such files by default and a whole cover went 422 over 5 bytes
# (The Dirt Beneath the Devil #3). Readers tolerate this; so do we.
ImageFile.LOAD_TRUNCATED_IMAGES = True

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
WIDTH_BUCKETS = (240, 480, 720, 1080, 1440, 2048)   # 240 = thumbnail strip, 480 = covers
COVER_WIDTH = 480
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
        try:
            os.utime(out)               # last-read time, for eviction
        except OSError:
            pass
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


READ_BUFFER = 4 << 20


class _Book:
    """Uniform read access over a CBZ or a CBR. Entry names are archive-relative."""

    def __init__(self, path: str, version: str, whole: bool = True):
        """whole=False is for a glance (the cover): a CBR is read entry by entry
        instead of unpacked. Covers used to unpack every CBR in full and keep it —
        853 of them, 61 GB of page cache for books nobody had opened."""
        self.path = path
        self._zip = self._dir = self._rar = self._fh = None
        with open(path, "rb") as fh:
            magic = fh.read(4)
        if magic[:2] == b"PK":
            # big reads: the share reaches the container through OrbStack, where
            # every small read is a slow round trip — zipfile's default buffering
            # made a file list 15 s and one page 10 s; 4 MB reads: 0.2 s and 1.6 s
            self._fh = open(path, "rb", buffering=READ_BUFFER)
            self._zip = zipfile.ZipFile(self._fh)
        elif magic == b"Rar!":
            out = _rar_dir(path, version)
            if whole or (os.path.isdir(out) and os.listdir(out)):
                self._dir = _extract_rar(path, version)
            else:
                import rarfile
                self._rar = rarfile.RarFile(path)
        else:
            raise ValueError("not a zip or rar archive")

    def names(self) -> list[str]:
        if self._zip:
            raw = self._zip.namelist()
        elif self._rar:
            raw = [i.filename for i in self._rar.infolist() if not i.is_dir()]
        else:
            raw = [os.path.relpath(os.path.join(r, f), self._dir)
                   for r, _, fs in os.walk(self._dir) for f in fs]
        return sorted((n for n in raw if _is_page(n)), key=_natural_key)

    def open(self, name: str):
        if self._zip:
            return self._zip.open(name)
        if self._rar:
            return io.BytesIO(self._rar.read(name))
        full = os.path.realpath(os.path.join(self._dir, name))
        if not full.startswith(os.path.realpath(self._dir) + os.sep):
            raise ValueError("entry escapes the archive")
        return open(full, "rb")

    def close(self):
        if self._zip:
            self._zip.close()
        if self._fh:
            self._fh.close()
        if self._rar:
            self._rar.close()


def _scan_pages(path: str, version: str, dims: bool = True) -> list[list]:
    """[[entry_name, width, height], ...] in reading order. Dimensions come from
    the image header only (Pillow opens lazily) — the reader uses them to show a
    wide spread on its own. dims=False lists the entries alone (0, 0): one read
    of the archive's directory instead of one per page, and a CBR isn't unpacked."""
    bk = _Book(path, version, whole=dims)
    try:
        if not dims:
            return [[name, 0, 0] for name in bk.names()]
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


_dims_pool = None


def _sizes_known(pages) -> bool:
    return any(w for _, w, _h in pages)


def _fill_dims_later(path: str, size, mtime):
    """Page sizes for a book opened quick: read in the background, one book at a
    time, and saved only if the file hasn't changed meanwhile."""
    from concurrent.futures import ThreadPoolExecutor
    global _dims_pool
    if _dims_pool is None:
        _dims_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="page-sizes")

    def run():
        try:
            pages = _scan_pages(path, _version(size, mtime))
            cur = db.get_book_by_path(path, DB_PATH)
            if pages and cur and cur["size"] == size and cur["mtime"] == mtime:
                db.upsert_book(path, size, mtime, pages, path=DB_PATH)
        except Exception as e:
            logger.info(f"Reader: page sizes for {path!r} not read: {e}")
    _dims_pool.submit(run)


def ensure_book(path: str, tracked_series_id=None, number=None, quick: bool = False) -> dict:
    """Register a file as a book, or return the existing row if unchanged.
    A changed size/mtime (re-download, cover injection) rebuilds the page list.

    quick (opening it to read): list the pages now and read their sizes in the
    background. Reading every page's header first was 46 reads into a 317 MB
    scan on a busy share — the Read button sat there for most of a minute."""
    try:
        st = os.stat(path)
    except OSError:
        raise HTTPException(404, "File is gone from the shelf")
    existing = db.get_book_by_path(path, DB_PATH)
    if existing and existing["size"] == st.st_size and existing["mtime"] == st.st_mtime and existing["pages"]:
        if quick and not _sizes_known(existing["pages"]):
            _fill_dims_later(path, st.st_size, st.st_mtime)      # an earlier quick open's sizes never landed
        return existing
    try:
        pages = _scan_pages(path, _version(st.st_size, st.st_mtime), dims=not quick)
    except Exception as e:
        logger.warning(f"Reader: can't open {path!r}: {e}")
        raise HTTPException(422, "This file can't be opened as a comic")
    if not pages:
        raise HTTPException(422, "This file has no pages")
    book_id = db.upsert_book(path, st.st_size, st.st_mtime, pages,
                             tracked_series_id=tracked_series_id, number=number, path=DB_PATH)
    if quick:
        _fill_dims_later(path, st.st_size, st.st_mtime)
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
            # each book's rendered pages, and each unpacked CBR, is its own entry:
            # '_rar' counted as ONE folder, touched on every unpack, so it was
            # always the newest thing here and the cap never reached it
            dirs = [d for d in os.scandir(PAGE_CACHE_DIR) if d.is_dir() and not d.name.startswith("_")]
            rar_root = os.path.join(PAGE_CACHE_DIR, "_rar")
            if os.path.isdir(rar_root):
                dirs += [d for d in os.scandir(rar_root) if d.is_dir() and not d.name.endswith(".tmp")]
            covers = os.path.join(PAGE_CACHE_DIR, "_covers")
            if os.path.isdir(covers):          # never evicted; counted so the cap is the real disk use
                total += sum(f.stat().st_size for f in os.scandir(covers) if f.is_file())
            for d in dirs:
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


def trim_cache() -> None:
    """Scheduler: hold the page cache to its cap even when nobody is reading."""
    _evict_if_needed()


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


def _grey_bottom_fraction(im) -> float:
    """How much of the image, from the bottom up, is the flat JPEG-grey a
    truncated decode leaves behind (every sample within 2 of 128)."""
    g = im.convert("L")
    w, h = g.size
    if not w or not h:
        return 0.0
    px = g.load()
    xs = range(0, w, max(1, w // 32))
    rows = 0
    for y in range(h - 1, -1, -1):
        if all(abs(px[x, y] - 128) <= 2 for x in xs):
            rows += 1
        else:
            break
    return rows / h


class TruncatedCover(ValueError):
    """Page 1 is a cut-short JPEG. The reader shows such a page anyway (grey
    below the break — LOAD_TRUNCATED_IMAGES), but as a COVER it must not be
    cached and served: the catalogue has the real thing. Batman: City of
    Madness #001's scan ends halfway down, and the half-grey tile stuck."""


def get_cover_bytes(path: str) -> bytes:
    """Page 1 at cover size, WITHOUT registering the book. The full open reads
    every page's header (282 for a Knightfall omnibus — 17 s cold); a library
    grid of 800 covers needs page 1 and nothing else. Raises TruncatedCover
    when page 1 is cut short, so callers fall through to the catalogue."""
    st = os.stat(path)
    ver = _version(st.st_size, st.st_mtime)
    key = re.sub(r"[^A-Za-z0-9]", "_", path)[-120:]
    cp = os.path.join(PAGE_CACHE_DIR, "_covers", f"{key}-{ver}-{COVER_WIDTH}.jpg")
    try:
        with open(cp, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    bk = _Book(path, ver, whole=False)
    try:
        names = bk.names()
        if not names:
            raise ValueError("no pages")
        with bk.open(names[0]) as fh, Image.open(fh) as im:
            # Strict decode first. A JPEG short by a few bytes is still a cover;
            # one that stops halfway is not — the lenient decode fills what's
            # missing with JPEG grey, so measure how much grey sits at the bottom.
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            try:
                im.load()
            except OSError as e:
                if "truncated" not in str(e).lower():
                    raise
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                im.load()
                if _grey_bottom_fraction(im) > 0.2:
                    raise TruncatedCover(path) from e
            finally:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if im.width > COVER_WIDTH:
                im = im.resize((COVER_WIDTH, max(1, round(im.height * COVER_WIDTH / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            data = buf.getvalue()
    finally:
        bk.close()
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    tmp = cp + f".{threading.get_ident()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, cp)
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
        "shelf_id": book.get("shelf_series_id"),
        "number": book.get("number"),
        "version": _version(book["size"], book["mtime"]),
        "page_count": book["page_count"],
        # wide = a two-page spread drawn as one image: shown on its own, never paired
        "pages": [{"w": w, "h": h, "wide": bool(w and h and w > h)} for _, w, h in book["pages"]],
        "finish_within_pages": FINISH_WITHIN_PAGES,
        "progress": db.get_progress(READER_ID, book["id"], DB_PATH),
        **_mark(book["id"]),
    }


def _mark(book_id: int) -> dict:
    from kometa.marks import get_mark
    try:
        return get_mark("book", book_id, DB_PATH)
    except Exception:
        return {"favourite": False, "rating": None}


def _book_or_404(book_id: int) -> dict:
    book = db.get_book(book_id, DB_PATH)
    if not book:
        raise HTTPException(404, "No such book")
    return book


# --- routes ---------------------------------------------------------------------

# Reading touches the share; nothing else should wait for it. A page off a 317 MB
# scan took 35–45 s with the NAS busy, and every one held a web worker: open one
# book (the reader fetches ahead) and the rest of the app queued behind it. Share
# reads now run on their own few threads; the routes await them, holding nothing.
# Two lanes: opening a book (what you just tapped) never queues behind the
# reader's fetch-ahead of pages you haven't reached.
SHARE_READERS = {"open": 2, "page": 4}
_share_pools: dict = {}


async def _on_share(fn, *a, lane: str = "page", **k):
    import asyncio
    import functools
    from concurrent.futures import ThreadPoolExecutor
    pool = _share_pools.get(lane)
    if pool is None:
        pool = _share_pools[lane] = ThreadPoolExecutor(max_workers=SHARE_READERS[lane], thread_name_prefix=f"share-{lane}")
    return await asyncio.get_running_loop().run_in_executor(pool, functools.partial(fn, *a, **k))


def _known_book(series_id: int, number: float) -> dict | None:
    """The indexed book for an issue, if it's already been opened (page list known)."""
    with db._connect(DB_PATH) as conn:
        r = conn.execute("SELECT id FROM books WHERE tracked_series_id = ? AND number = ? AND page_count > 0 "
                         "ORDER BY mtime DESC LIMIT 1", (series_id, number)).fetchone()
    return db.get_book(r["id"], DB_PATH) if r else None


def _issue_book_on_share(series_id: int, number: float) -> dict:
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404, "No such series")
    # the shelf index already knows the file — no folder listing on the share
    with db._connect(DB_PATH) as conn:
        r = conn.execute("SELECT path FROM books WHERE tracked_series_id = ? AND number = ? ORDER BY mtime DESC LIMIT 1",
                         (series_id, number)).fetchone()
    if r:
        try:
            return ensure_book(r["path"], tracked_series_id=series_id, number=number, quick=True)
        except HTTPException as e:
            if e.status_code != 404:
                raise                                    # moved since the last scan: look in the folder
    path = find_issue_file(s.get("folder_path"), s["title"], number)
    if not path:
        raise HTTPException(404, "No file on the shelf for this issue")
    return ensure_book(path, tracked_series_id=series_id, number=number, quick=True)


@router.get("/api/series/{series_id}/issues/{number}/book")
async def issue_book(series_id: int, number: float):
    """The book behind an owned issue — the 'Read' button's first hop. A book
    opened before comes straight from the index (the shelf scan notices a changed
    file); only a first open lists the folder and reads the page sizes."""
    from starlette.concurrency import run_in_threadpool
    book = await run_in_threadpool(_known_book, series_id, number)
    if not book:
        book = await _on_share(_issue_book_on_share, series_id, number, lane="open")
    return await run_in_threadpool(_book_payload, book)


@router.get("/api/books/{book_id}")
async def book_detail(book_id: int):
    from starlette.concurrency import run_in_threadpool
    book = await run_in_threadpool(_book_or_404, book_id)
    if not (book.get("pages") and book.get("page_count")):          # never opened: read its pages off the share
        book = await _on_share(ensure_book, book["path"], book.get("tracked_series_id"), book.get("number"), quick=True, lane="open")
    return await run_in_threadpool(_book_payload, book)


def _cached_page(book: dict, index: int, width: int) -> bytes | None:
    try:
        with open(_page_cache_path(book, index, width), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        os.utime(os.path.join(PAGE_CACHE_DIR, str(book["id"])))
    except OSError:
        pass
    return data


@router.get("/api/books/{book_id}/pages/{page}")
async def book_page(book_id: int, page: int, w: int | None = None):
    """Page `page` (1-based), resized to the bucket covering `w`. The client
    appends ?v=<version> so a re-downloaded file busts the browser cache —
    which is why the response can be cached as immutable. A page already in the
    local cache never queues behind share reads."""
    from starlette.concurrency import run_in_threadpool
    from kometa.activity import note_reading
    note_reading()
    book = await run_in_threadpool(_book_or_404, book_id)
    if not 1 <= page <= (book["page_count"] or 0):
        raise HTTPException(404, "No such page")
    width = _bucket(w)
    data = await run_in_threadpool(_cached_page, book, page - 1, width)
    if data is None:
        try:
            data = await _on_share(get_page_bytes, book, page - 1, width)
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
