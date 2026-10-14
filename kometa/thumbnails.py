"""Cover art pipeline — every thumbnail route and the disk cache behind them.

Extracted from main.py (which had grown to ~1,950 lines of routes + arc logic +
this). Fully self-contained: main includes `router` and nothing else in the app
imports from here. The design rule this module enforces: an external cover is
fetched from LOCG/S3 AT MOST ONCE, then lives on disk next to the DB and
in the browser cache (30d max-age) — a grid render must never turn into a
per-tile network expedition.

The record's own store (kometa/images.py, kometa/covers.py) comes first — page 1
of the file you own, made by Kometa — then the catalogue. Komga was a last resort
behind a flag until it was retired (2026-10-12); the posters harvested from it
before then are local store rows, still served first.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
import hashlib
import logging

import requests as _requests
from fastapi import APIRouter, HTTPException, Request, Response

import kometa.db as db

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



# Covers in the browser: a day, then refetched in the background while the old
# one still shows. It was 30 days, so a cover fixed on the server (City of
# Madness's half-grey page 1) stayed broken in the browser for a month.
COVER_MAX_AGE = 86400


def _store_response(key: str, source: str, max_age: int = COVER_MAX_AGE) -> Response | None:
    """Bytes the record's store holds for a key — file covers, Komga posters —
    with the source named in the headers so a tile can say where it came from."""
    from kometa import coverupgrade, images
    p = coverupgrade.better_path(key, DB_PATH) or images.local_path(key, DB_PATH)   # a bigger copy of the same cover first
    if not p:
        return None
    try:
        data = open(p, "rb").read()
    except OSError:
        return None
    if not data:
        return None
    return Response(content=data, media_type=_img_ct(data),
                    headers={"Cache-Control": f"public, max-age={max_age}, stale-while-revalidate=604800", "X-Kometa-Image": "disk", "X-Kometa-Source": source,
                             "ETag": images.file_etag(p)})


def _tag(resp, source: str):
    if resp is not None and hasattr(resp, "headers") and "X-Kometa-Source" not in resp.headers:
        resp.headers["X-Kometa-Source"] = source
    return resp


# --- don't-wait covers (Activity) ---------------------------------------------------
# A cover nobody has made yet is page 1 of a file on the comics share. With the NAS
# busy that's 1–6 s a cover, and a list of fifty rows parks the browser's six
# connections on them — the page's own API calls queue behind the art. ?fast=1:
# a cover that's ready comes straight back; one that isn't gets a 204 in under half
# a second and keeps cooking in the background, so it's in the store next time.
FAST_WAIT_S = 0.4
_bg = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cover")
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _within(key: str, fn):
    with _inflight_lock:
        fut = _inflight.get(key)
        if fut is None:
            # carry the request's context across: thread pools don't, and without
            # it these covers waited on LOCG's pace like a background job
            import contextvars
            fut = _bg.submit(contextvars.copy_context().run, fn)
            _inflight[key] = fut
            fut.add_done_callback(lambda _f: _inflight.pop(key, None))
    try:
        return fut.result(timeout=FAST_WAIT_S)
    except _FutureTimeout:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/api/series/{series_id}/thumbnail")
def series_thumbnail(series_id: int, fast: int = 0):
    if fast:
        return _within(f"s{series_id}", lambda: _series_thumbnail(series_id))
    return _series_thumbnail(series_id)


def _series_thumbnail(series_id: int):
    """The run's card. Order: a poster you set in Komga (harvested into the store
    before it was retired) → page 1 of the lowest owned issue, from the store →
    the catalogue's cover from the store → the catalogue live → a trade's cover →
    page 1 made on the spot."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    from kometa import covers, images
    resp = _store_response(covers.series_komga_key(series_id), "komga-poster")
    if resp:
        return resp
    resp = _store_response(covers.series_file_key(series_id), "file")
    if resp:
        return resp
    # Use cached issue image URLs from DB — avoids live source API calls under concurrent grid load
    issues = db.get_issues_for_series(series_id, DB_PATH)
    img_url = next(
        (i["metron_image"] for i in sorted(issues, key=lambda x: x["number"])
         if i.get("metron_image")),
        None
    )
    if img_url:
        # the record's own store (kometa/images.py): on disk by key, kept for good
        resp = images.serve(images.series_key(series_id), img_url, "metron", path=DB_PATH)
        if resp:
            return _tag(resp, "catalogue")
        return _tag(_cached_image_response(img_url), "catalogue")
    # No issues with art at all — a collections-only LOCG entry ('Batman: Bad
    # Seeds' is just its TPB + HC) painted a black void on the card. Its trades
    # have covers; use the first real edition's.
    cached = db.get_trades(series_id, DB_PATH)
    trade_cover = next((t["cover"] for t in (cached or {}).get("trades", [])
                        if t.get("cover") and not t.get("is_variant")), None)
    if trade_cover:
        return _tag(_cached_image_response(trade_cover), "catalogue")
    # Nothing from the store or the catalogues — a shelf-imported series waiting
    # to be matched. Page 1 of its first file, made by Kometa, kept in the store.
    resp = _shelf_cover_response(series_id)
    if resp:
        return _tag(resp, "file")
    raise HTTPException(404)


def _jpeg(data: bytes) -> Response:
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


def _shelf_cover_response(series_id: int) -> Response | None:
    """Page 1 of the series' first file, made now and kept in the store."""
    from kometa import covers, shelf
    shelf_id = db.shelf_id_for_series(series_id, DB_PATH)
    first = shelf_id and shelf._first_book(shelf_id)
    if not first:
        return None
    key = covers.series_file_key(series_id)
    try:
        data = covers.cover_bytes_for_file(key, first["path"], DB_PATH, note="shelf")
        if data:
            return _store_response(key, "file") or _tag(_jpeg(data), "file")
    except Exception as e:
        logger.warning(f"shelf cover failed for series {series_id}: {e}")
    return None


@router.get("/api/series/{series_id}/issues/{number}/thumbnail")
def issue_thumbnail(series_id: int, number: float, request: Request = None, fast: int = 0):
    if fast:
        return _within(f"i{series_id}/{number:g}", lambda: _issue_thumbnail(series_id, number, request))
    return _issue_thumbnail(series_id, number, request)


def _issue_thumbnail(series_id: int, number: float, request: Request = None):
    """The issue's tile. Order: your chosen variant → page 1 of the file you own,
    from the store (made on the spot the first time) → the catalogue's cover from
    the store → the catalogue live → LOCG's variant art."""
    issue = next(iter(db.get_issues_for_series(series_id, DB_PATH, number=number)), None)
    from kometa import covers, images

    # Your chosen variant wins — an explicit pick beats the file's own cover, the
    # same precedence the issue tile and library card use.
    vc = issue.get("variant_cover") if issue else None
    if vc and vc.startswith("http"):
        resp = _image_or_none(vc)
        if resp:
            return _tag(resp, "variant")

    # Owned: page 1 of the file, from the store; made now if the trickle hasn't
    # reached it. A page 1 that stops halfway is remembered as failed and the
    # catalogue's cover takes over below.
    if issue and issue.get("owned"):
        key = covers.file_key(series_id, number)
        resp = _store_response(key, "file")
        if resp:
            return resp
        row = images.get(key, DB_PATH)
        if not (row and row.get("failed_at")):
            from kometa.naming import find_issue_file
            s = db.get_series_by_id(series_id, DB_PATH)
            fp = s and find_issue_file(s.get("folder_path"), s["title"], number)
            data = fp and covers.cover_bytes_for_file(key, fp, DB_PATH)
            if data:
                try:
                    covers._series_file_cover(series_id, float(number), DB_PATH)
                except Exception:
                    pass
                return _store_response(key, "file") or _tag(_jpeg(data), "file")

    # Known-artless issue: 404 immediately (with browser caching) instead of
    # re-running the whole LOCG chain on every grid render. Without this,
    # each scroll past an artless tile re-fires S3 misses and LOCG lookups.
    miss_key = (series_id, number)
    if _thumb_misses.get(miss_key, 0) > time.time():
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})

    # The catalogue's cover (legacy-named metron_image column) — skip the 'no cover'
    # placeholders some rows carry (relative paths that can never load)
    mi = issue.get("metron_image") if issue else None
    if mi and mi.startswith("http") and "no-cover" not in mi:
        resp = images.serve(images.issue_key(series_id, number), mi, "metron", path=DB_PATH) or _image_or_none(mi)
        if resp:
            return _tag(resp, "catalogue")

    # Artless issues often have variant art on LOCG before the main cover is
    # posted (upcoming issues). covers[0] is the main, so it gets first shot;
    # otherwise the first variant with real art wins. Cached by URL.
    locg_iid = issue.get("locg_issue_id") if issue else None
    if locg_iid:
        try:
            from kometa.locg_client import fetch_variants
            data = fetch_variants(locg_iid)
            for c in data.get("covers", [])[:6]:
                resp = _image_or_none(c.get("thumb"))
                if resp:
                    return _tag(resp, "catalogue")
        except Exception as e:
            # Transient failure (LOCG hiccup, CF challenge, timeout) is NOT a
            # verdict on whether art exists — return a plain uncached 404 so the
            # next render gets a fresh attempt.
            logger.warning(f"thumbnail fallback failed for series {series_id} #{number}: {e}")
            return Response(status_code=404)

    # Clean determination: every source genuinely has no art right now. Remember
    # that so the next render doesn't pay for the same expedition; new art gets
    # another chance after the TTL.
    _thumb_misses[miss_key] = time.time() + _THUMB_MISS_TTL
    return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})
