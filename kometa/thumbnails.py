"""Cover art pipeline — every thumbnail route and the disk cache behind them.

Extracted from main.py (which had grown to ~1,950 lines of routes + arc logic +
this). Fully self-contained: main includes `router` and nothing else in the app
imports from here. The design rule this module enforces: an external cover is
fetched from Komga/LOCG/S3 AT MOST ONCE, then lives on disk next to the DB and
in the browser cache (30d max-age) — a grid render must never turn into a
per-tile network expedition.
"""
import os
import time
import hashlib
import logging

import requests as _requests
from fastapi import APIRouter, HTTPException, Request, Response

import kometa.db as db
from kometa.sources import komga as _komga
from kometa.naming import parse_issue_number as _parse_issue_number

logger = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = db.DB_PATH

# Auth-free session for fetching CDN images (S3 rejects Basic auth headers)
_img_session = _requests.Session()
_img_session.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Cover image cache — lives on the same volume as the DB (persistent on the NAS).
_COVER_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "cover-cache")


def _img_ct(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _cache_path(cache_key: str) -> str:
    # SHA1 is a filename hash here, NOT a security digest — usedforsecurity=False
    # says so (and keeps it working on FIPS builds). Collision resistance is
    # irrelevant: worst case two cover URLs share a cache file, which just means
    # a re-fetch.
    return os.path.join(_COVER_CACHE_DIR, hashlib.sha1(cache_key.encode("utf-8"), usedforsecurity=False).hexdigest())


def _read_cached(path: str) -> bytes | None:
    """Read cached bytes off disk, or None on miss/empty/unreadable."""
    if not os.path.exists(path):
        return None
    try:
        data = open(path, "rb").read()
        return data or None
    except OSError:
        return None


def _write_cached(path: str, data: bytes) -> None:
    """Atomically stash bytes on disk (temp + rename). Best-effort — a failed
    write just means we re-fetch next time, no reason to blow up the request."""
    try:
        os.makedirs(_COVER_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        pass


def _image_or_none(url: str, max_age: int = 2592000):
    """Serve a cover image, caching the bytes on disk so each external cover is
    fetched at most once. Cache-Control lets the browser keep it too, so flipping
    pages doesn't re-hit this endpoint at all. Returns None instead of raising —
    callers chain fallback sources."""
    if not url:
        return None
    headers = {"Cache-Control": f"public, max-age={max_age}"}
    path = _cache_path(url)
    cached = _read_cached(path)
    if cached:                                      # cache hit — serve from disk
        return Response(content=cached, media_type=_img_ct(cached), headers=headers)
    try:                                            # miss — fetch once, store, serve
        r = _img_session.get(url, timeout=8)
        if r.ok and r.content:
            _write_cached(path, r.content)
            return Response(content=r.content,
                            media_type=r.headers.get("content-type", "image/jpeg"),
                            headers=headers)
    except Exception:
        pass
    return None


def _cached_image_response(url: str, max_age: int = 2592000):
    resp = _image_or_none(url, max_age)
    if resp:
        return resp
    raise HTTPException(404)


# Issues whose entire cover-fallback chain struck out: (series_id, number) →
# don't-retry-before timestamp. Keeps artless tiles from re-running LOCG/S3
# lookups on every grid render.
_THUMB_MISS_TTL = 6 * 3600  # seconds
_thumb_misses: dict = {}


def _cached_bytes(cache_key: str, fetch, max_age: int = 2592000):
    """Disk-cache image bytes under a stable key (not a URL). `fetch` is a
    zero-arg callable returning (bytes, content_type) or None. This is what lets
    Komga thumbnails be pulled ONCE and then served off disk forever — without it
    every grid render fires a live Komga round-trip per cover and the threadpool
    drowns. Returns a Response or None (caller decides the fallback)."""
    headers = {"Cache-Control": f"public, max-age={max_age}"}
    path = _cache_path(cache_key)
    cached = _read_cached(path)
    if cached:                                      # cache hit — serve from disk
        return Response(content=cached, media_type=_img_ct(cached), headers=headers)
    try:                                            # miss — fetch once, store, serve
        got = fetch()
        if got:
            data, ct = got
            if data:
                _write_cached(path, data)
                return Response(content=data, media_type=ct or _img_ct(data), headers=headers)
    except Exception:
        pass
    return None


def _komga_thumb(komga, url: str, cache_key: str):
    """Fetch a Komga thumbnail through the disk cache. Returns a Response or None.
    Komga builds its thumbnail from page 1 too, so a scan that stops halfway
    gives a half-grey tile from Komga as surely as from us (City of Madness
    #001). Refuse those — and forget a cached one — so the catalogue cover wins."""
    def fetch():
        r = komga.session.get(url, timeout=8)
        if r.ok and r.content and not _half_grey(r.content):
            return r.content, r.headers.get("content-type", "image/jpeg")
        return None
    cached = _read_cached(_cache_path(cache_key))
    if cached and _half_grey(cached):
        try:
            os.remove(_cache_path(cache_key))
        except OSError:
            pass
    return _cached_bytes(cache_key, fetch)


def _half_grey(data: bytes) -> bool:
    """A thumbnail whose bottom fifth or more is flat JPEG grey came from a
    truncated page; it is not a cover."""
    try:
        from PIL import Image
        import io
        from kometa.reader import _grey_bottom_fraction
        with Image.open(io.BytesIO(data)) as im:
            return _grey_bottom_fraction(im) > 0.2
    except Exception:
        return False


# numberSort -> book_id maps, cached per Komga series with a short TTL. WITHOUT this,
# issue_thumbnail fired a FULL get_books() against Komga for every issue that lacked a
# komga_book_id — so a 20-cover grid where covers haven't been linked yet meant 20 full
# book-list fetches, and issues with no Komga match re-fetched on every single render
# forever. One fetch per series per TTL window now, shared across the whole grid.
_BOOK_MAP_CACHE: "dict[str, tuple[float, dict]]" = {}
_BOOK_MAP_TTL = 300  # seconds


def _komga_book_map(komga, komga_series_id: str, title: str = "") -> dict:
    now = time.time()
    hit = _BOOK_MAP_CACHE.get(komga_series_id)
    if hit and now - hit[0] < _BOOK_MAP_TTL:
        return hit[1]
    try:
        # Filename is truth; Komga's numberSort lies (e.g. a lone "Noir #003" gets
        # numberSort 1.0, which would mis-map issue #1 onto it). Mirror sync.py's map:
        # parse the filename first, numberSort only as fallback, filename wins clashes.
        m, src = {}, {}
        for b in komga.get_books(komga_series_id):
            if b.get("media", {}).get("status") == "ERROR":
                continue
            fn = _parse_issue_number(b.get("name", ""), title)
            if fn is not None:
                key, s = fn, "name"
            else:
                n = b.get("metadata", {}).get("numberSort")
                if n is None:
                    continue
                key, s = float(n), "sort"
            if key in m and not (src[key] == "sort" and s == "name"):
                continue
            m[key], src[key] = b["id"], s
        _BOOK_MAP_CACHE[komga_series_id] = (now, m)
        return m
    except Exception:
        return hit[1] if hit else {}


@router.get("/api/series/{series_id}/thumbnail")
def series_thumbnail(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    komga = _komga()
    if s.get("komga_series_id") and komga:
        try:
            resp = _komga_thumb(
                komga,
                f"{komga.base_url}/api/v1/series/{s['komga_series_id']}/thumbnail",
                f"komga:series:{s['komga_series_id']}",
            )
            if resp:
                return resp
        except Exception:
            pass
    # Use cached issue image URLs from DB — avoids live source API calls under concurrent grid load
    issues = db.get_issues_for_series(series_id, DB_PATH)
    img_url = next(
        (i["metron_image"] for i in sorted(issues, key=lambda x: x["number"])
         if i.get("metron_image")),
        None
    )
    if img_url:
        # the record's own store (kometa/images.py): on disk by key, kept for good
        from kometa import images
        resp = images.serve(images.series_key(series_id), img_url, "metron", path=DB_PATH)
        if resp:
            return resp
        return _cached_image_response(img_url)
    # No issues with art at all — a collections-only LOCG entry ('Batman: Bad
    # Seeds' is just its TPB + HC) painted a black void on the card. Its trades
    # have covers; use the first real edition's.
    cached = db.get_trades(series_id, DB_PATH)
    trade_cover = next((t["cover"] for t in (cached or {}).get("trades", [])
                        if t.get("cover") and not t.get("is_variant")), None)
    if trade_cover:
        return _cached_image_response(trade_cover)
    # Nothing from Komga, LOCG or trades — a shelf-imported series waiting to be
    # matched. Page 1 of its first file, made by Kometa.
    resp = _shelf_cover_response(series_id)
    if resp:
        return resp
    raise HTTPException(404)


def _jpeg(data: bytes) -> Response:
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


def _file_cover_response(series_id: int, number: float, request: Request | None = None) -> Response | None:
    """The cover of the file you own. Validated, not cached blind: the ETag is
    the file's size+mtime, the browser checks back each time and gets a 304
    when nothing changed — so a re-downloaded file shows its new cover at once
    (2026-10-08: Batgirls #2/#11 replaced, grid kept the old page for 30 days)."""
    from kometa.naming import find_issue_file
    from kometa import reader
    s = db.get_series_by_id(series_id, DB_PATH)
    path = s and find_issue_file(s.get("folder_path"), s["title"], number)
    if not path:
        return None
    try:
        st = os.stat(path)
        etag = f'"{st.st_size}-{int(st.st_mtime)}"'
        if request is not None and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return Response(content=reader.get_cover_bytes(path), media_type="image/jpeg",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})
    except Exception as e:
        logger.warning(f"file cover failed for series {series_id} #{number}: {e}")
        return None


def _shelf_cover_response(series_id: int) -> Response | None:
    from kometa import reader, shelf
    shelf_id = db.shelf_id_for_series(series_id, DB_PATH)
    first = shelf_id and shelf._first_book(shelf_id)
    if not first:
        return None
    try:
        return _jpeg(reader.get_cover_bytes(first["path"]))
    except Exception as e:
        logger.warning(f"shelf cover failed for series {series_id}: {e}")
        return None


@router.get("/api/series/{series_id}/issues/{number}/thumbnail")
def issue_thumbnail(series_id: int, number: float, request: Request = None):
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)

    # Your chosen variant wins — an explicit pick beats Komga's file cover, the
    # same precedence the issue tile and library card use. (Also dodges a stale
    # Komga book lingering after a file's been deleted/replaced.)
    vc = issue.get("variant_cover") if issue else None
    if vc and vc.startswith("http"):
        resp = _image_or_none(vc)
        if resp:
            return resp

    # Owned: the cover of the FILE, read by Kometa itself — ahead of Komga, whose
    # thumbnail lags a re-download until its own rescan. Validated per request.
    if issue and issue.get("owned"):
        resp = _file_cover_response(series_id, number, request)
        if resp:
            return resp

    book_id = issue.get("komga_book_id") if issue else None
    komga = _komga()

    # Stale cache — live-lookup from Komga (via the TTL'd book map, so a grid of
    # un-linked covers shares ONE get_books instead of one per cover) and write back
    # so future calls hit the DB directly.
    if not book_id and komga:
        series = db.get_series_by_id(series_id, DB_PATH)
        komga_series_id = series.get("komga_series_id") if series else None
        if komga_series_id:
            title = series.get("title", "") if series else ""
            book_id = _komga_book_map(komga, komga_series_id, title).get(number)
            if book_id:
                # Stamp ONLY the book id. Finding a Komga book does NOT mean the issue
                # is owned on disk — ownership is folder-truth. (The old code set
                # owned=True here, which falsely marked un-downloaded issues as owned.)
                db.set_komga_book_id(series_id, number, book_id, DB_PATH)

    if book_id and komga:
        try:
            resp = _komga_thumb(
                komga,
                f"{komga.base_url}/api/v1/books/{book_id}/thumbnail",
                _book_cache_key(book_id, issue.get("komga_book_v") if issue else None),
            )
            if resp:
                return resp
        except Exception:
            pass

    # Known-artless issue: 404 immediately (with browser caching) instead of
    # re-running the whole LOCG chain on every grid render. Without this,
    # each scroll past an artless tile re-fires S3 misses and LOCG lookups.
    miss_key = (series_id, number)
    if _thumb_misses.get(miss_key, 0) > time.time():
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})

    # LOCG list art (legacy-named metron_image column) — skip the 'no cover' placeholders some rows carry
    # (relative paths that can never load; older syncs stored them as-is)
    mi = issue.get("metron_image") if issue else None
    if mi and mi.startswith("http") and "no-cover" not in mi:
        from kometa import images
        resp = images.serve(images.issue_key(series_id, number), mi, "metron", path=DB_PATH) or _image_or_none(mi)
        if resp:
            return resp

    # Last resort: artless issues often have variant art on LOCG before the main
    # cover is posted (looking at you, upcoming issues). covers[0] is the main, so
    # it gets first shot; otherwise the first variant with real art wins. The
    # variant fetch is cached (6h) and each found image is disk-cached by URL.
    locg_iid = issue.get("locg_issue_id") if issue else None
    if locg_iid:
        try:
            from kometa.locg_client import fetch_variants
            data = fetch_variants(locg_iid)
            for c in data.get("covers", [])[:6]:
                resp = _image_or_none(c.get("thumb"))
                if resp:
                    return resp
        except Exception as e:
            # Transient failure (LOCG hiccup, CF challenge, timeout) is NOT a
            # verdict on whether art exists — return a plain uncached 404 so the
            # next render gets a fresh attempt. Caching an exception as "no art"
            # is how a one-off blip becomes a 6-hour blank tile.
            logger.warning(f"thumbnail fallback failed for series {series_id} #{number}: {e}")
            return Response(status_code=404)

    # Clean determination: every source genuinely has no art right now. Remember
    # that so the next render doesn't pay for the same expedition; new art gets
    # another chance after the TTL.
    _thumb_misses[miss_key] = time.time() + _THUMB_MISS_TTL
    return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})


def _book_cache_key(book_id: str, v: str | None) -> str:
    # Versioned: a replaced file under the same book id is a cache MISS, not
    # the old cover served off disk forever. Unversioned keeps the old key so
    # existing cache entries stay warm for callers that don't know a version.
    return f"komga:book:{book_id}:{v}" if v else f"komga:book:{book_id}"


@router.get("/api/book/{book_id}/thumbnail")
def book_thumbnail(book_id: str, v: str | None = None):
    komga = _komga()
    if not komga:
        raise HTTPException(404)
    try:
        resp = _komga_thumb(
            komga,
            f"{komga.base_url}/api/v1/books/{book_id}/thumbnail",
            _book_cache_key(book_id, v),
        )
        if resp:
            return resp
        raise HTTPException(404)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(504) from e
