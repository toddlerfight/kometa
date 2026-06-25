import os
import json
import hashlib
import logging
import threading
import time
from datetime import date
from contextlib import asynccontextmanager

import requests as _requests

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.komga_client import KomgaClient
from kometa.metron_client import MetronClient
from kometa.locg_client import search_series_anon as _locg_search_anon, get_issue_details_anon as _locg_issue_details, get_trades_anon as _locg_trades, select_editions as _select_editions
from kometa.scheduler import start_scheduler
import kometa.db as db
from kometa.sources import (
    komga as _komga, metron as _metron,
    locg as _locg, comics_root as _comics_root, comicvine as _comicvine,
)
from kometa.naming import (
    find_issue_file as _find_issue_file, normalize_url as _normalize_url, norm as _norm,
    _resolve_dir, parse_issue_number as _parse_issue_number,
)
from kometa.sync import (
    sync_one as _sync_one, rescan_owned as _rescan_owned,
    _best_komga_match, _komga_all_series,
    enrich_trades as _enrich_trades,
)
from kometa.acquisition import (
    get_progress, get_search_status,
    _process_queue, _sweep_missing,
    _poll_usenet_jobs, _poll_torrent_jobs, _release_day_retry,
)

logger = logging.getLogger(__name__)

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
    return os.path.join(_COVER_CACHE_DIR, hashlib.sha1(cache_key.encode("utf-8")).hexdigest())


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
    """Fetch a Komga thumbnail through the disk cache. Returns a Response or None."""
    def fetch():
        r = komga.session.get(url, timeout=8)
        if r.ok and r.content:
            return r.content, r.headers.get("content-type", "image/jpeg")
        return None
    return _cached_bytes(cache_key, fetch)


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


def _sync_all_job():
    for s in db.get_all_series(DB_PATH):
        _sync_one(s)
    # Sweep AFTER every series has been folder-scanned above, so `owned` reflects
    # disk before we decide what's missing. _sweep_missing is folder-gated — it only
    # touches series whose folder we've actually inventoried, so the old "fresh
    # instance with no folders → sweep the entire catalog" blowup can't recur. Genuine
    # gaps in collections we've verified get queued; everything else is left alone.
    _sweep_missing()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    # Recover items orphaned by a mid-flight container restart
    db.reset_stuck_queue_items(DB_PATH)
    start_scheduler(_sync_all_job, _process_queue, _release_day_retry, _poll_usenet_jobs, _poll_torrent_jobs)
    yield


app = FastAPI(lifespan=lifespan)


# --- sync logic ---

def _summary(issues):
    from datetime import timedelta
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    owned = sum(1 for r in issues if r["owned"])
    missing = sum(1 for r in issues if not r["owned"] and (not r["store_date"] or r["store_date"] < today))
    upcoming = sum(1 for r in issues if not r["owned"] and r["store_date"] and r["store_date"] >= today)
    soon = [r["store_date"] for r in issues
            if not r["owned"] and r["store_date"] and today <= r["store_date"] <= cutoff]
    return {"owned": owned, "missing": missing, "upcoming": upcoming,
            "next_release": min(soon) if soon else None}


# --- connection tests ---

# Test endpoints accept creds in-body but fall back to STORED config — secrets
# never round-trip through the browser, so 'Test' must work against what's saved.
def _stored(key: str) -> str:
    return db.get_config(DB_PATH).get(key, "")


class TestKomgaRequest(BaseModel):
    url: str | None = None
    user: str | None = None
    password: str | None = None


@app.post("/api/test/komga")
def test_komga(req: TestKomgaRequest):
    url = req.url or _stored("komga_url")
    user = req.user or _stored("komga_user")
    password = req.password or _stored("komga_pass")
    if not (url and user and password):
        return {"ok": False, "error": "Not configured"}
    try:
        client = KomgaClient(base_url=_normalize_url(url), auth=(user, password))
        r = client.session.get(f"{client.base_url}/api/v1/libraries", timeout=8)
        r.raise_for_status()
        raw = r.json()
        libs = raw if isinstance(raw, list) else raw.get("content", [])
        return {"ok": True, "libraries": [{"id": lib["id"], "name": lib["name"]} for lib in libs]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestMetronRequest(BaseModel):
    user: str | None = None
    password: str | None = None


@app.post("/api/test/metron")
def test_metron(req: TestMetronRequest):
    user = req.user or _stored("metron_user")
    password = req.password or _stored("metron_pass")
    if not (user and password):
        return {"ok": False, "error": "Not configured"}
    try:
        client = MetronClient(auth=(user, password))
        r = client.session.get(f"{client.base_url}/series/", params={"name": "batman", "page": 1}, timeout=10)
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestLocgRequest(BaseModel):
    user: str | None = None
    password: str | None = None


@app.post("/api/test/locg")
def test_locg(req: TestLocgRequest):
    user = req.user or _stored("locg_user")
    password = req.password or _stored("locg_pass")
    if not (user and password):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.locg_client import LOCGClient
        LOCGClient(user, password)  # constructor logs in; raises on bad creds
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestSabRequest(BaseModel):
    url: str | None = None
    apikey: str | None = None


@app.post("/api/test/sab")
def test_sab(req: TestSabRequest):
    url = req.url or _stored("sab_url")
    apikey = req.apikey or _stored("sab_apikey")
    if not (url and apikey):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.sabnzbd_client import SABnzbdClient
        data = SABnzbdClient(url, apikey)._api(mode="queue")
        if data.get("status") is False:
            return {"ok": False, "error": data.get("error", "API rejected")}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Integration → config keys it owns. Drives 'Disconnect': blank password fields
# mean 'keep current', so without this there is NO path to remove a credential.
_INTEGRATION_KEYS = {
    "komga":     ["komga_url", "komga_user", "komga_pass", "komga_library_id"],
    "metron":    ["metron_user", "metron_pass"],
    "locg":      ["locg_user", "locg_pass"],
    "sabnzbd":   ["sab_url", "sab_apikey"],
}


@app.post("/api/config/disconnect/{integration}")
def disconnect_integration(integration: str):
    keys = _INTEGRATION_KEYS.get(integration)
    if not keys:
        raise HTTPException(404)
    db.set_config({k: "" for k in keys}, DB_PATH)
    return get_config()


# --- config ---

@app.get("/api/config")
def get_config():
    cfg = db.get_config(DB_PATH)
    indexers_raw = cfg.get("newznab_indexers", "[]")
    try:
        indexers = json.loads(indexers_raw)
    except Exception:
        indexers = []
    # Strip apikeys from indexer list before returning
    safe_indexers = [{"name": i["name"], "host": i["host"], "ssl": i.get("ssl", True)} for i in indexers]
    root = _comics_root()
    return {
        "comics_root":         root,
        # Usable = exists and writable. Drives the just-in-time folder prompt:
        # a properly-mounted deploy is ok and never gets nagged.
        "comics_root_ok":      os.path.isdir(root) and os.access(root, os.W_OK),
        "komga_url":           cfg.get("komga_url", ""),
        "komga_user":          cfg.get("komga_user", ""),
        "komga_pass":          "",
        "komga_library_id":    cfg.get("komga_library_id", ""),
        "metron_user":         cfg.get("metron_user", ""),
        "metron_pass":         "",
        "locg_user":           cfg.get("locg_user", ""),
        "locg_pass":           "",
        "locg_configured":     bool(cfg.get("locg_user", "") and cfg.get("locg_pass", "")),
        "sync_hours":          cfg.get("sync_hours", "5,12,17"),
        "sab_url":             cfg.get("sab_url", ""),
        "sab_configured":      bool(cfg.get("sab_url", "") and cfg.get("sab_apikey", "")),
        "newznab_indexers":    safe_indexers,
    }


class ConfigRequest(BaseModel):
    comics_root:        str | None = None
    komga_url:          str | None = None
    komga_user:         str | None = None
    komga_pass:         str | None = None
    komga_library_id:   str | None = None
    metron_user:        str | None = None
    metron_pass:        str | None = None
    locg_user:          str | None = None
    locg_pass:          str | None = None
    sync_hours:         str | None = None
    sab_url:            str | None = None
    sab_apikey:         str | None = None
    newznab_indexers:   str | None = None  # JSON array of {name, host, apikey, ssl}
    qbit_url:           str | None = None
    qbit_user:          str | None = None
    qbit_pass:          str | None = None
    prowlarr_url:       str | None = None
    prowlarr_apikey:    str | None = None


@app.patch("/api/config")
def update_config(req: ConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None and v != ""}
    if "komga_url" in updates:
        updates["komga_url"] = _normalize_url(updates["komga_url"])
    db.set_config(updates, DB_PATH)
    return get_config()


def _load_indexers() -> list[dict]:
    try:
        return json.loads(db.get_config(DB_PATH).get("newznab_indexers", "[]"))
    except Exception:
        return []


def _save_indexers(indexers: list[dict]):
    db.set_config({"newznab_indexers": json.dumps(indexers)}, DB_PATH)


class IndexerRequest(BaseModel):
    name: str
    host: str
    apikey: str
    ssl: bool = True


@app.post("/api/config/indexers", status_code=201)
def add_indexer(req: IndexerRequest):
    # Add/remove operate on individual entries (not a whole-list re-save) so the
    # stored apikeys are never round-tripped through the browser and blanked.
    indexers = _load_indexers()
    indexers.append({"name": req.name, "host": req.host, "apikey": req.apikey, "ssl": req.ssl})
    _save_indexers(indexers)
    return {"ok": True, "count": len(indexers)}


@app.delete("/api/config/indexers/{idx}", status_code=204)
def remove_indexer(idx: int):
    indexers = _load_indexers()
    if not (0 <= idx < len(indexers)):
        raise HTTPException(404)
    indexers.pop(idx)
    _save_indexers(indexers)


# --- series routes ---

@app.get("/api/series")
def list_series():
    series = db.get_all_series(DB_PATH)
    summaries = db.get_all_series_summaries(DB_PATH)
    empty = {"owned": 0, "missing": 0, "upcoming": 0, "next_release": None, "card_image": None}
    return [dict(s, **summaries.get(s["id"], empty)) for s in series]


def _cached_trades(series: dict) -> list[dict] | None:
    """Read the enriched trade cache (owned + komga_book_id stamped at sync time).
    Self-heals a pre-enrichment cache once, so reads stay fold-scan-free after."""
    cached = db.get_trades(series["id"], DB_PATH)
    if not cached:
        return None
    trades = cached["trades"]
    if trades and "owned" not in trades[0]:
        _enrich_trades(series, trades)
        db.set_trades(series["id"], trades, DB_PATH)
    return trades


@app.get("/api/series/{series_id}")
def get_series(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    # Badge = UNOWNED trades, read from the stored owned flag (no per-request scan).
    # None until first sync populates the cache, so it stays hidden vs flashing 0.
    trades = _cached_trades(s)
    trade_count = sum(1 for t in trades if not t["owned"]) if trades else None
    # has_trades = does this series have ANY collected edition (owned or not). Distinct
    # from trade_count (the unowned-only badge): a trade-only series whose sole edition
    # is already owned still has_trades, so the UI lands on Trades instead of an empty
    # Issues grid that polls 'Syncing issues…' forever for singles that don't exist.
    has_trades = bool(trades)
    return dict(s, issues=issues, trade_count=trade_count, has_trades=has_trades, **_summary(issues))


@app.get("/api/series/{series_id}/trades")
def get_series_trades(series_id: int):
    """Collected editions (TPB/HC) available for this series, from LOCG. Discovery
    only — LOCG carries the trade's name, not the issues it collects, so there's no
    range here yet (that's a later confirm step). Variant printings are folded out
    by default; pass ?variants=1 to see them."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    locg_id = s["locg_series_id"]
    if not locg_id:
        return {"trades": [], "reason": "no_locg_id"}
    # Read the cache sync populated; only hit LOCG live when it's missing or stale
    # (>24h). Trades drift slowly — new editions get solicited over weeks, not hours.
    cached = db.get_trades(series_id, DB_PATH)
    if cached and cached["age"] < 24 * 3600:
        return {"trades": _cached_trades(s), "cached": True}
    # Cold/stale — fetch, enrich (owned + komga) once, store. The enrich here is the
    # periodic scan, not a per-request one; warm reads above never touch the disk.
    trades = _select_editions(_locg_trades(locg_id))
    _enrich_trades(s, trades)
    db.set_trades(series_id, trades, DB_PATH)
    return {"trades": trades, "cached": False}


@app.get("/api/trade/{locg_id}/details")
def get_trade_details(locg_id: str):
    """Description + credits for a collected edition — same LOCG page scrape the
    issue modal uses, just addressed by the trade's own comic id."""
    try:
        return _locg_issue_details(locg_id)
    except Exception as e:
        raise HTTPException(502, f"LOCG details failed: {e}")


class TradeDownloadRequest(BaseModel):
    locg_id: str
    title: str
    vol: int | None = None
    vol_range: list[int] | None = None
    cover: str | None = None
    edition_title: str | None = None


@app.post("/api/series/{series_id}/trades/download")
def download_trade(series_id: int, req: TradeDownloadRequest):
    """Queue a collected edition — exactly like queueing an issue, kind='trade'.
    It flows through the same worker (GetComics + usenet, civility, parking) and
    shows up in Activity. The file lands and Komga scans it; issues are not
    reconciled (folder is truth)."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    if not s["folder_path"]:
        return {"queued": False, "reason": "no_folder"}
    db.queue_trade(series_id, req.locg_id, req.title, vol=req.vol,
                   vol_range=req.vol_range, cover=req.cover,
                   edition_title=req.edition_title, path=DB_PATH)
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"queued": True}


class AddSeriesRequest(BaseModel):
    metron_id: int | None = None
    locg_id: int | None = None
    cv_arc_id: int | None = None
    folder_path: str | None = None
    komga_id: str | None = None
    on_pull_list: bool = True
    # Metadata from LOCG/ComicVine when metron_id is absent
    title: str | None = None
    publisher_name: str | None = None
    year_began: int | None = None


def _add_arc(req: AddSeriesRequest):
    """Add a story arc: a kind='arc' tracked_series whose cross-title reading order
    is populated from ComicVine. Reuses folder/queue/Komga machinery; the arc's
    issues span titles so they live in arc_issues, not issue_status."""
    cv = _comicvine()
    title = req.title or ""
    publisher = req.publisher_name or "DC Comics"
    folder = req.folder_path or _resolve_dir(_comics_root(), publisher or "Unknown", title)
    new_id = db.add_series(
        title=title, publisher=publisher, year_began=req.year_began,
        folder_path=folder, on_pull_list=req.on_pull_list,
        kind="arc", cv_arc_id=str(req.cv_arc_id), path=DB_PATH,
    )
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg():
        if not cv:
            return
        try:
            issues = [{"reading_order": r["order"], "source_title": r["series"],
                       "number": r["number"], "story_title": r["title"],
                       "cv_issue_id": str(r["cv_issue_id"])}
                      for r in cv.get_arc_issues(req.cv_arc_id)]
            db.replace_arc_reading_order(new_id, issues, DB_PATH)
            logger.info(f"Arc {title!r}: populated {len(issues)} reading-order issues from CV")
            # Phase E adds: resolve owned/komga_book_id against Komga, populate trades,
            # and (if on_pull_list) grab the covering trades through the cascade.
        except Exception as e:
            logger.warning(f"Arc populate failed for {title!r}: {e}")

    threading.Thread(target=_bg, daemon=True).start()
    return added


@app.post("/api/series", status_code=201)
def add_series(req: AddSeriesRequest):
    if req.cv_arc_id:
        return _add_arc(req)
    metron = _metron()

    title = req.title or ""
    publisher = req.publisher_name
    year_began = req.year_began
    metron_series_id = req.metron_id
    locg_series_id = req.locg_id
    folder_path = req.folder_path
    komga_series_id = req.komga_id

    if req.metron_id:
        # Metron-sourced: fetch canonical metadata
        ms = metron.get_series(req.metron_id)
        title = ms.get("name") or ms.get("series_name") or title
        pub = ms.get("publisher")
        publisher = pub.get("name") if isinstance(pub, dict) else (pub or publisher)
        year_began = ms.get("year_began") or year_began
    else:
        # LOCG-sourced: try to auto-link to Metron by title — but only if Metron
        # is configured. Without creds this is a guaranteed 401 we'd just swallow.
        cfg = db.get_config(DB_PATH)
        if cfg.get("metron_user") and cfg.get("metron_pass"):
            try:
                candidates = metron.search_series(title)
                match = next(
                    (r for r in candidates
                     if _norm(r.get("series") or r.get("name") or "") == _norm(title)),
                    None
                )
                if match:
                    metron_series_id = match["id"]
                    pub = match.get("publisher")
                    publisher = pub.get("name") if isinstance(pub, dict) else (pub or publisher)
                    year_began = match.get("year_began") or year_began
            except Exception:
                pass

    komga = _komga()
    if komga_series_id and komga:
        try:
            ks = komga.get_series(komga_series_id)
            title = ks.get("name") or title
            publisher = ks.get("metadata", {}).get("publisher") or publisher
            if not folder_path:
                folder_path = ks.get("url")
        except Exception:
            pass
    elif not komga_series_id and komga:
        # Auto-link to Komga via the SAME single-exact-match rule sync uses
        # (_best_komga_match) — refuses ambiguous titles (e.g. multiple Batman runs)
        # instead of grabbing the first loose lowercase hit.
        try:
            komga_series_id = _best_komga_match(_komga_all_series(komga), title)
        except Exception:
            pass

    # No folder yet (no Komga, or Komga had none)? Derive it from publisher+title.
    # _resolve_dir finds an existing on-disk folder (variation-tolerant) or returns
    # the canonical new path, so the first sync reconciles owned-vs-missing correctly
    # whether or not the series is already on disk. This is what makes Komga optional.
    if not folder_path:
        folder_path = _resolve_dir(_comics_root(), publisher or "Unknown", title)

    new_id = db.add_series(
        komga_series_id, metron_series_id,
        title=title,
        publisher=publisher,
        year_began=year_began,
        folder_path=folder_path,
        on_pull_list=req.on_pull_list,
        locg_series_id=locg_series_id,
        path=DB_PATH,
    )
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg_sync():
        _sync_one(added)
        if req.on_pull_list:
            issues = db.get_issues_for_series(new_id, DB_PATH)
            today_str = str(date.today())
            for issue in issues:
                if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today_str):
                    db.queue_issue(new_id, issue["number"], DB_PATH)
            _process_queue()

    threading.Thread(target=_bg_sync, daemon=True).start()
    return added


@app.delete("/api/series/{series_id}", status_code=204)
def delete_series(series_id: int):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    db.remove_series(series_id, DB_PATH)


class PullListRequest(BaseModel):
    on_pull_list: bool


@app.patch("/api/series/{series_id}/pull-list", status_code=200)
def toggle_pull_list(series_id: int, req: PullListRequest):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    db.set_pull_list(series_id, req.on_pull_list, DB_PATH)
    return db.get_series_by_id(series_id, DB_PATH)


# --- search ---

_STOP_WORDS = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "is", "it",
               "as", "by", "be", "or", "and", "but", "from", "with", "this", "that",
               "not", "are", "was", "were", "has", "have", "had", "its", "here", "there"}

def _metron_search_ranked(metron, q: str) -> list[dict]:
    results = metron.search_series(q)
    if results:
        return [dict(r, source="metron") for r in results]
    # Metron is punctuation-sensitive ("whats" ≠ "What's").
    # Retry with word pairs then single words, filtering to results that
    # contain every meaningful query word in their normalized name.
    words = [w for w in _norm(q).split() if len(w) > 2 and w not in _STOP_WORDS]
    if words:
        def _relevant(hits):
            return [r for r in hits if all(
                w in _norm(r.get('series') or r.get('name') or '')
                for w in words
            )]
        if len(words) >= 2:
            for i in range(len(words) - 1):
                good = _relevant(metron.search_series(f"{words[i]} {words[i+1]}"))
                if good:
                    return [dict(r, source="metron") for r in good]
        for word in sorted(words, key=len, reverse=True):
            good = _relevant(metron.search_series(word))
            if good:
                return [dict(r, source="metron") for r in good]
    return []


@app.get("/api/search/metron")
def search_metron(q: str):
    # Metron is optional. Not configured (no creds) or a transient failure both
    # return [] rather than 500, so the wizard falls through to LOCG cleanly
    # instead of dying on the Metron call. This is what makes key-free onboarding work.
    cfg = db.get_config(DB_PATH)
    if not (cfg.get("metron_user") and cfg.get("metron_pass")):
        return []
    try:
        return _metron_search_ranked(_metron(), q)
    except Exception as e:
        logger.warning(f"Metron search failed for {q!r}: {e}")
        return []


@app.get("/api/search/locg")
def search_locg(q: str):
    locg_client = _locg()
    raw = locg_client.search_series(q) if locg_client else _locg_search_anon(q)
    return [{
        "id":         r["id"],
        "series":     r["title"],
        "publisher":  {"name": r["publisher"]} if r["publisher"] else None,
        "year_began": r["year"],
        "cover":      r.get("cover"),
        "source":     "locg",
    } for r in raw[:15]]


@app.get("/api/search/comicvine")
def search_comicvine(q: str):
    # The gap-filler: only reached when LOCG comes up empty (vintage events /
    # collections it doesn't catalog). Not configured → [] so the wizard degrades
    # cleanly. Same result shape as LOCG, carrying cv_volume_id for the add path.
    cv = _comicvine()
    if not cv:
        return []
    out = []
    try:
        # Arcs first — when something falls through to CV it's usually an event /
        # collection, and the arc is the whole cross-title story in reading order.
        for a in cv.search_arcs(q):
            out.append({
                "id":         a["cv_arc_id"],
                "series":     (a["name"] or "").replace('"', ''),   # CV names like '"Batman" Knightfall'
                "publisher":  {"name": a["publisher"]} if a["publisher"] else None,
                "cv_arc_id":  a["cv_arc_id"],
                "kind":       "arc",
                "source":     "comicvine",
            })
        for r in cv.search_volumes(q):
            out.append({
                "id":           r["cv_volume_id"],
                "series":       r["name"],
                "publisher":    {"name": r["publisher"]} if r["publisher"] else None,
                "year_began":   r["year"],
                "issue_count":  r["issue_count"],
                "cv_volume_id": r["cv_volume_id"],
                "kind":         "series",
                "source":       "comicvine",
            })
    except Exception as e:
        logger.warning(f"ComicVine search failed for {q!r}: {e}")
    return out


# --- sync ---

@app.post("/api/sync")
def sync_all():
    threading.Thread(target=_sync_all_job, daemon=False).start()
    return {"ok": True, "started": True}


@app.post("/api/sync/{series_id}")
def sync_one(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    threading.Thread(target=_sync_one, args=(s,), daemon=True).start()
    return {"ok": True}


# --- pull list ---

@app.get("/api/pull-list")
def pull_list(days: int = 90, past: int = 0):
    return db.get_upcoming_issues(days, past, DB_PATH)


# --- thumbnails ---

@app.get("/api/series/{series_id}/thumbnail")
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
    # Use cached issue image URLs from DB — avoids live Metron API calls under concurrent grid load
    issues = db.get_issues_for_series(series_id, DB_PATH)
    img_url = next(
        (i["metron_image"] for i in sorted(issues, key=lambda x: x["number"])
         if i.get("metron_image")),
        None
    )
    if img_url:
        return _cached_image_response(img_url)
    raise HTTPException(404)


@app.get("/api/series/{series_id}/issues/{number}/thumbnail")
def issue_thumbnail(series_id: int, number: float):
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
                f"komga:book:{book_id}",
            )
            if resp:
                return resp
        except Exception:
            pass

    # Known-artless issue: 404 immediately (with browser caching) instead of
    # re-running the whole metron/LOCG chain on every grid render. Without this,
    # each scroll past an artless tile re-fires S3 misses and LOCG lookups.
    miss_key = (series_id, number)
    if _thumb_misses.get(miss_key, 0) > time.time():
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})

    # Metron/LOCG list art — skip the 'no cover' placeholders some rows carry
    # (relative paths that can never load; older syncs stored them as-is)
    mi = issue.get("metron_image") if issue else None
    if mi and mi.startswith("http") and "no-cover" not in mi:
        resp = _image_or_none(mi)
        if resp:
            return resp

    # Last resort: artless issues often have variant art on LOCG before the main
    # cover is posted (looking at you, upcoming issues). covers[0] is the main, so
    # it gets first shot; otherwise the first variant with real art wins. The
    # variant fetch is cached (6h) and each found image is disk-cached by URL.
    locg_iid = issue.get("locg_issue_id") if issue else None
    if locg_iid:
        try:
            locg = _locg()
            if locg:
                data = locg.fetch_variants(locg_iid)
            else:
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


@app.get("/api/book/{book_id}/thumbnail")
def book_thumbnail(book_id: str):
    komga = _komga()
    if not komga:
        raise HTTPException(404)
    try:
        resp = _komga_thumb(
            komga,
            f"{komga.base_url}/api/v1/books/{book_id}/thumbnail",
            f"komga:book:{book_id}",
        )
        if resp:
            return resp
        raise HTTPException(404)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(504) from e


# --- metron thumbnails ---

@app.get("/api/metron/series/{metron_id}/thumbnail")
def metron_series_thumbnail(metron_id: int):
    metron = _metron()
    try:
        detail = metron.get_series(metron_id)
        img_url = detail.get("image")

        if not img_url:
            raise HTTPException(404)

        return _cached_image_response(img_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404) from e


@app.get("/api/metron/series/{metron_id}/info")
def metron_series_info(metron_id: int):
    metron = _metron()
    try:
        detail = metron.get_series(metron_id)
        return {
            "id":          metron_id,
            "issue_count": detail.get("issue_count"),
            "volume":      detail.get("volume"),
            "series_type": (detail.get("series_type") or {}).get("name", "") if isinstance(detail.get("series_type"), dict) else detail.get("series_type", ""),
        }
    except Exception as e:
        raise HTTPException(404) from e


# --- filesystem browse ---

def _fs_browse_default() -> str:
    """Where the filesystem picker should land — not the raw '/' full of OS guts.
    Prefer an existing comics root (the mount you're likely picking), else the
    home dir (where a desktop user keeps files), else '/'."""
    cr = os.path.realpath(_comics_root())
    if os.path.isdir(cr):
        return cr
    home = os.path.expanduser("~")
    return home if (home != "~" and home != "/" and os.path.isdir(home)) else "/"


@app.get("/api/fs/browse")
def browse_fs(path: str = "", scope: str = "library"):
    # 'library' stays sandboxed to the comics root (picking a series subfolder).
    # 'fs' browses the whole filesystem — needed to pick the comics root itself,
    # which by definition isn't inside the (maybe missing) comics root yet. It
    # still lands somewhere friendly (see _fs_browse_default), not bare '/'.
    if scope == "fs":
        root = "/"
        default = _fs_browse_default()
    else:
        root = default = os.path.realpath(_comics_root())
    target = os.path.realpath(path or default)
    if not target.startswith(root):
        raise HTTPException(403)
    if not os.path.isdir(target):
        raise HTTPException(404)
    try:
        names = sorted(
            n for n in os.listdir(target)
            if not n.startswith('.') and os.path.isdir(os.path.join(target, n))
        )
    except PermissionError:
        names = []
    parent = os.path.dirname(target)
    return {
        "path": target,
        "parent": parent if parent.startswith(root) and parent != target else None,
        "dirs": names,
    }


class MkdirRequest(BaseModel):
    path: str          # the directory to create the new folder inside
    name: str          # new folder name (single level, no separators)
    scope: str = "library"


@app.post("/api/fs/mkdir", status_code=201)
def fs_mkdir(req: MkdirRequest):
    """Create a subfolder while browsing — the 'New Folder' button. Same scope
    sandbox as browse; one level only (no separators/traversal)."""
    root = "/" if req.scope == "fs" else os.path.realpath(_comics_root())
    parent = os.path.realpath(req.path or root)
    if not parent.startswith(root):
        raise HTTPException(403)
    if not os.path.isdir(parent):
        raise HTTPException(404, "Parent folder not found")
    if not os.access(parent, os.W_OK):
        raise HTTPException(403, "That folder isn't writable")
    name = req.name.strip().strip("/")
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(422, "Invalid folder name")
    target = os.path.join(parent, name)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Could not create folder: {e}") from e
    return {"path": target}


@app.get("/api/fs/resolve")
def resolve_folder(publisher: str = "", title: str = ""):
    """Preview where a series will be filed — the same publisher+title resolution
    add_series uses. Reports whether that folder already exists on disk so the UI
    can say 'existing' vs 'new'."""
    path = _resolve_dir(_comics_root(), publisher or "Unknown", title)
    return {"path": path, "exists": os.path.isdir(path)}


# --- folder path ---

class FolderRequest(BaseModel):
    folder_path: str | None = None


@app.patch("/api/series/{series_id}/folder")
def set_folder(series_id: int, req: FolderRequest):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    db.set_folder_path(series_id, req.folder_path or None, DB_PATH)
    s = db.get_series_by_id(series_id, DB_PATH)
    _rescan_owned(s)   # scan the just-set folder → mark which issues are owned
    return db.get_series_by_id(series_id, DB_PATH)



# --- download queue ---

@app.get("/api/queue")
def get_queue():
    items = db.get_queue(DB_PATH)
    for item in items:
        prog = get_progress(item["id"])
        if prog:
            item["progress"] = prog
        if item["state"] == "searching":
            ss = get_search_status(item["id"])
            if ss:
                item["search_status"] = ss
    return items


@app.delete("/api/queue/{queue_id}", status_code=204)
def delete_queue_item(queue_id: int):
    db.remove_queue_item(queue_id, DB_PATH)


@app.post("/api/queue/retry-not-found")
def retry_not_found():
    """Bulk re-search of everything that came up empty — pull-to-refresh on
    Activity. failed items are excluded; they have per-row Retry."""
    n = db.requeue_not_found(DB_PATH)
    if n:
        threading.Thread(target=_process_queue, daemon=True).start()
    return {"requeued": n}


@app.post("/api/queue/{queue_id}/retry", status_code=200)
def retry_queue_item(queue_id: int):
    db.update_queue_state(queue_id, "queued", error=None, path=DB_PATH)
    db.reset_rl_attempts(queue_id, path=DB_PATH)
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


@app.post("/api/series/{series_id}/search-missing")
def search_missing(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    today = str(date.today())
    queued = 0
    for issue in issues:
        if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today):
            db.queue_issue(series_id, issue["number"], DB_PATH)
            queued += 1
    if queued:
        threading.Thread(target=_process_queue, daemon=True).start()
    return {"queued": queued}


@app.post("/api/series/{series_id}/issues/{number}/search")
def search_issue(series_id: int, number: float):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    db.queue_issue(series_id, number, DB_PATH)
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/sweep")
def manual_sweep():
    def _sweep_and_process():
        _sweep_missing()
        _process_queue()
    threading.Thread(target=_sweep_and_process, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/process")
def manual_process():
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/clear-history", status_code=204)
def clear_queue_history():
    db.clear_queue_history(DB_PATH)


@app.get("/api/series/{series_id}/issues/{number}/metron")
def get_issue_metron(series_id: int, number: float):
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue or not issue.get("metron_issue_id"):
        raise HTTPException(404)
    try:
        detail = _metron().get_issue(issue["metron_issue_id"])
        return {
            "desc":       detail.get("desc"),
            "title":      detail.get("name") or detail.get("title", ""),
            "credits":    detail.get("credits", []),
            "characters": [c.get("name", "") for c in detail.get("characters", [])],
            "arcs":       [a.get("name", "") for a in detail.get("arcs", [])],
        }
    except Exception as e:
        raise HTTPException(404) from e


@app.get("/api/series/{series_id}/issues/{number}/locg-details")
def get_issue_locg_details(series_id: int, number: float):
    """Description + credits from LOCG (keyless). Cached per locg_issue_id; the
    external kometa-recommend project also consumes this cache."""
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue or not issue.get("locg_issue_id"):
        raise HTTPException(404)
    lid = issue["locg_issue_id"]
    cached = db.get_issue_details_cache(lid, DB_PATH)
    if cached is not None:
        return cached
    try:
        detail = _locg_issue_details(lid)
    except Exception as e:
        raise HTTPException(502, "LOCG details fetch failed") from e
    db.set_issue_details_cache(lid, detail, DB_PATH)
    return detail


@app.get("/api/series/{series_id}/issues/{number}/queue-status")
def get_issue_queue_status(series_id: int, number: float):
    queue = db.get_queue(DB_PATH)
    q = next((q for q in queue if q["tracked_series_id"] == series_id and q["issue_number"] == number), None)
    if not q:
        return {"state": None}
    result = {"state": q["state"], "error": q.get("error")}
    prog = get_progress(q["id"])
    if prog:
        result["progress"] = prog
    return result


@app.get("/api/series/{series_id}/issues/{number}/variants")
def get_issue_variants(series_id: int, number: float):
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue:
        raise HTTPException(404)
    # Saved pick (if any) so the Variants tab reflects what was chosen last time
    # instead of resetting to a blank slate on every reopen.
    prefs = db.get_variant_prefs(series_id, number, DB_PATH)
    sel_ids = [c.get("id") for c in prefs["selected"]] if prefs else []
    primary = prefs["primary_id"] if prefs else None

    locg_issue_id = issue.get("locg_issue_id")
    if not locg_issue_id:
        return {"covers": [], "locg_issue_id": None, "selected_ids": sel_ids, "primary_id": primary}
    try:
        locg = _locg()
        if locg:
            data = locg.fetch_variants(locg_issue_id)
        else:
            from kometa.locg_client import fetch_variants
            data = fetch_variants(locg_issue_id)
        return {"covers": data["covers"], "locg_issue_id": locg_issue_id,
                "selected_ids": sel_ids, "primary_id": primary}
    except Exception as e:
        raise HTTPException(502, detail=str(e)) from e


class VariantApplyRequest(BaseModel):
    selected: list
    primary_id: str


@app.post("/api/series/{series_id}/issues/{number}/variants/apply")
def apply_issue_variants(series_id: int, number: float, req: VariantApplyRequest):
    if not req.selected:
        raise HTTPException(400, detail="No variants selected")

    series = db.get_series_by_id(series_id, DB_PATH)
    if not series:
        raise HTTPException(404)

    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue:
        raise HTTPException(404)

    if issue.get("owned"):
        file_path = _find_issue_file(series.get("folder_path", ""), series["title"], number)
        if not file_path:
            raise HTTPException(404, detail="File not found on disk")
        try:
            from kometa.downloader import inject_covers
            added = inject_covers(file_path, req.selected, req.primary_id)
            # Persist the pick as a display override too. The CBZ now has the cover,
            # but Komga's thumbnail (what we display) is frozen until it re-scans — so
            # stamp variant_cover so Kometa shows YOUR pick immediately regardless.
            db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
            # Nudge Komga to re-extract the cover from the rewritten file so ITS
            # thumbnail + reader catch up to the new page 1 too. Best-effort — the file
            # and our own display are already correct whether or not this succeeds.
            book_id = issue.get("komga_book_id")
            komga = _komga()
            if book_id and komga:
                try:
                    komga.analyze_book(book_id)
                except Exception as e:
                    logger.warning(f"Komga re-analyze failed for book {book_id}: {e}")
            return {"ok": True, "added": added}
        except Exception as e:
            raise HTTPException(500, detail=str(e)) from e
    else:
        db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
        return {"ok": True, "queued": True}


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
