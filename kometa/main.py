import os
import re
import json
import logging
import threading
from datetime import date
from contextlib import asynccontextmanager


from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.komga_client import KomgaClient
from kometa.locg_client import search_series_anon as _locg_search_anon, get_issue_details_anon as _locg_issue_details, get_trades_anon as _locg_trades, select_editions as _select_editions, resolve_comic_series_anon as _locg_resolve_comic_anon
from kometa.models import AddSeriesRequest
from kometa.arcs import (
    router as _arcs_router, _owned_collection, _add_arc,
)
from kometa.scheduler import start_scheduler, last_scheduled_sync_utc
import kometa.db as db
from kometa.sources import (
    komga as _komga,
    locg as _locg, comics_root as _comics_root, comicvine as _comicvine,
)
from kometa.naming import (
    find_issue_file as _find_issue_file, normalize_url as _normalize_url, _resolve_dir,
)
from kometa.sync import (
    sync_one as _sync_one, sync_one_guarded, full_sync_lock,
    rescan_owned as _rescan_owned,
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

def _sync_all_job():
    # One full sweep at a time. A deploy near a cron hour fires BOTH the startup
    # catch-up and the scheduler; without this they interleave 47 series' worth
    # of SQLite writes and someone hits "database is locked".
    if not full_sync_lock.acquire(blocking=False):
        logger.info("Full sync already running — skipping this invocation")
        return
    try:
        for s in db.get_all_series(DB_PATH):
            # Guarded per series: a bad series logs and the loop MARCHES ON —
            # one LOCG hiccup used to abort the whole sweep, and with it the
            # _sweep_missing pass and the last_full_sync stamp below.
            sync_one_guarded(s, _sync_one)
        # Sweep AFTER every series has been folder-scanned above, so `owned` reflects
        # disk before we decide what's missing. _sweep_missing is folder-gated — it only
        # touches series whose folder we've actually inventoried, so the old "fresh
        # instance with no folders → sweep the entire catalog" blowup can't recur. Genuine
        # gaps in collections we've verified get queued; everything else is left alone.
        _sweep_missing()
        # Stamped at the END on purpose: a sync that crashes mid-run reads as "still
        # missed" and the next startup catch-up (lifespan, below) retries it.
        from datetime import datetime, timezone
        db.set_config({"last_full_sync": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}, DB_PATH)
    finally:
        full_sync_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    # Recover items orphaned by a mid-flight container restart
    db.reset_stuck_queue_items(DB_PATH)
    start_scheduler(_sync_all_job, _process_queue, _release_day_retry, _poll_usenet_jobs, _poll_torrent_jobs)
    # Missed-sync catch-up. The scheduler's jobstore is in-memory, so a restart
    # (i.e. every deploy) that straddles a cron fire produces a process that has
    # NO IDEA the fire was missed — apscheduler's misfire grace can't help across
    # restarts. Compare the last completed full sync against the most recent
    # scheduled slot; if we slept through it, run it now. This is what finally
    # kills the "deploy near a sync hour = pull list silently skips a day" trap.
    missed_slot = last_scheduled_sync_utc()
    if missed_slot and db.get_config(DB_PATH).get("last_full_sync", "") < missed_slot:
        logger.info(f"Startup catch-up: missed scheduled sync at {missed_slot} UTC — running now")
        threading.Thread(target=_sync_all_job, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

# Cover-art pipeline (thumbnail routes + disk cache) lives in kometa/thumbnails —
# fully self-contained; nothing else in the app imports from it.
from kometa.thumbnails import router as _thumbnails_router  # noqa: E402
app.include_router(_thumbnails_router)
# Story-arc machinery + routes live in kometa/arcs (imported at the top with the
# three functions main's own routes call back into).
app.include_router(_arcs_router)


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


class TestQbitRequest(BaseModel):
    url: str | None = None
    user: str | None = None
    password: str | None = None


@app.post("/api/test/qbit")
def test_qbit(req: TestQbitRequest):
    url = req.url or _stored("qbit_url")
    user = req.user or _stored("qbit_user")
    password = req.password or _stored("qbit_pass")
    if not (url and user and password):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.qbittorrent_client import QBittorrentClient
        ok, detail = QBittorrentClient(url, user, password).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": detail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestProwlarrRequest(BaseModel):
    url: str | None = None
    apikey: str | None = None


@app.post("/api/test/prowlarr")
def test_prowlarr(req: TestProwlarrRequest):
    url = req.url or _stored("prowlarr_url")
    apikey = req.apikey or _stored("prowlarr_apikey")
    if not (url and apikey):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.prowlarr_client import ProwlarrClient
        ok, detail = ProwlarrClient(url, apikey).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": detail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Integration → config keys it owns. Drives 'Disconnect': blank password fields
# mean 'keep current', so without this there is NO path to remove a credential.
_INTEGRATION_KEYS = {
    "komga":     ["komga_url", "komga_user", "komga_pass", "komga_library_id"],
    "locg":      ["locg_user", "locg_pass"],
    "sabnzbd":   ["sab_url", "sab_apikey"],
    "qbit":      ["qbit_url", "qbit_user", "qbit_pass"],
    "prowlarr":  ["prowlarr_url", "prowlarr_apikey"],
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
        "locg_user":           cfg.get("locg_user", ""),
        "locg_pass":           "",
        "locg_configured":     bool(cfg.get("locg_user", "") and cfg.get("locg_pass", "")),
        "sync_hours":          cfg.get("sync_hours", "5,12,17"),
        "sab_url":             cfg.get("sab_url", ""),
        "sab_configured":      bool(cfg.get("sab_url", "") and cfg.get("sab_apikey", "")),
        "newznab_indexers":    safe_indexers,
        "qbit_url":            cfg.get("qbit_url", ""),
        "qbit_user":           cfg.get("qbit_user", ""),
        "qbit_pass":           "",
        "qbit_configured":     bool(cfg.get("qbit_url", "") and cfg.get("qbit_user", "")),
        "prowlarr_url":        cfg.get("prowlarr_url", ""),
        "prowlarr_apikey":     "",
        "prowlarr_configured": bool(cfg.get("prowlarr_url", "") and cfg.get("prowlarr_apikey", "")),
    }


class ConfigRequest(BaseModel):
    comics_root:        str | None = None
    komga_url:          str | None = None
    komga_user:         str | None = None
    komga_pass:         str | None = None
    komga_library_id:   str | None = None
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
    # Arcs are a navigation lens, not library items — they're reached via a
    # series' Arcs tab + their own detail page, not as cards in the grid.
    series = [s for s in db.get_all_series(DB_PATH) if s.get("kind") != "arc"]
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
    if s.get("kind") == "arc":
        # An arc's cross-title reading order lives in arc_issues, not issue_status.
        arc_issues = db.get_arc_reading_order(series_id, DB_PATH)
        owned = sum(1 for i in arc_issues if i["owned"])
        # 'collected' = a trade that collects this arc is ACTUALLY OWNED ON DISK. Disk
        # is the only ownership source — a Komga metadata entry named after the arc does
        # NOT count (that was the old lie: a "collected" arc whose trade had no files).
        coll = _owned_collection(s["title"])
        collection = ({"name": coll["title"], "series_id": coll["id"],
                       "komga_series_id": coll.get("komga_series_id")} if coll else None)
        # Origin run = where the arc starts (its first issue's run) — the back-link
        # target, since the arc lives under that series' Arcs tab.
        origin = None
        if arc_issues:
            f = arc_issues[0]
            run = (db.get_series_by_cv_volume(f.get("cv_volume_id"), DB_PATH) if f.get("cv_volume_id") else None) \
                or db.find_series_by_title(f.get("source_title", ""), DB_PATH)
            if run:
                origin = {"series_id": run["id"], "title": run["title"]}
        return dict(s, arc_issues=arc_issues, issues=[],
                    owned=owned, missing=len(arc_issues) - owned, upcoming=0,
                    collected=bool(coll), collection=collection,
                    origin=origin, has_trades=False, trade_count=None)
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
    # arc_count badge: prefer the cached discovered-arc count (no Wikipedia fetch on
    # page load); fall back to tracked-arc count until the Arcs tab warms the cache.
    disc = db.get_arc_discovery(series_id, DB_PATH)
    if disc:
        arc_count = len(disc["arcs"]) or None
    else:
        from kometa.arc import arc_includes_series
        arc_count = sum(1 for a in db.get_all_arcs(DB_PATH)
                        if arc_includes_series(a["source_titles"], s["title"])) or None
    return dict(s, issues=issues, trade_count=trade_count, has_trades=has_trades,
                arc_count=arc_count, **_summary(issues))






























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
























@app.post("/api/series", status_code=201)
def add_series(req: AddSeriesRequest):
    if req.cv_arc_id:
        return _add_arc(req)
    # Following a storyline lands its ORIGIN RUN as a normal series, anchored to the
    # CV volume so the Arcs tab can scope to that run later. Idempotent two ways:
    #   1) exact anchor match — same volume already followed → return it.
    #   2) title+year match — the run is already tracked but carries a stale/missing
    #      anchor (the old title+year resolver mis-stamped reprint volumes, e.g.
    #      Batman 1940 ← Novaro 126840). Re-stamp it to THIS authoritative origin
    #      volume and return it, rather than minting a twin. Self-healing.
    # Year disambiguates same-named runs (Batman 1940 vs 2025), so this won't collapse
    # distinct volumes together.
    if req.cv_volume_id:
        existing = db.get_series_by_cv_volume(str(req.cv_volume_id), DB_PATH)
        if existing:
            return existing
        from kometa.arc import base_series_title, titles_match
        want = base_series_title(req.title or "")
        for s in db.get_all_series(DB_PATH):
            if s.get("kind") == "arc":
                continue
            if (s.get("year_began") == req.year_began
                    and want and titles_match(s.get("title") or "", want)):
                db.set_series_cv_volume(s["id"], str(req.cv_volume_id), DB_PATH)
                return db.get_series_by_id(s["id"], DB_PATH)
    title = req.title or ""
    publisher = req.publisher_name
    year_began = req.year_began
    locg_series_id = req.locg_id
    folder_path = req.folder_path
    komga_series_id = req.komga_id

    # One-shot backstop: if the client forwards a comic id + slug, the picked result was
    # an ISSUE, not a series. Resolve it to its parent series HERE, server-side, so a
    # comic id can never be persisted as a series anchor (which yields get_issues == 0
    # → a zombie 'Syncing…' forever). Authoritative: overrides whatever locg_id the
    # client computed. A one-shot that isn't linked to a series is refused, not stored.
    if req.locg_comic_id and req.locg_comic_slug:
        locg_client = _locg()
        sid = (locg_client.resolve_comic_series(req.locg_comic_id, req.locg_comic_slug)
               if locg_client
               else _locg_resolve_comic_anon(req.locg_comic_id, req.locg_comic_slug))
        if not sid:
            raise HTTPException(400, "This one-shot isn't linked to a series on LOCG")
        locg_series_id = sid
        title = re.sub(r"\s*#\s*\d+.*$", "", title).strip()  # drop the '#1' issue suffix

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

    # Materialize the folder NOW, at add time — don't wait for the first download to
    # create it. A tracked series with no folder on disk is invisible to _sweep_missing
    # (its folder gate skips unscanned series), so a genuinely-new series could never
    # get its FIRST grab: no folder → not swept → nothing to create the folder. Creating
    # it here breaks that chicken-and-egg: rescan_owned scans an empty dir (correctly: 0
    # owned), the sweep gate passes, and the pull list actually fills it. Existing folders
    # (resolved or user-picked in the wizard) are left alone — scanned, ownership honored,
    # no blind re-grab. Best-effort: a mkdir failure just falls back to the old behavior.
    if folder_path and not os.path.isdir(folder_path):
        try:
            os.makedirs(folder_path, exist_ok=True)
            logger.info(f"Created folder for new series {title!r}: {folder_path}")
        except OSError as e:
            logger.warning(f"Could not create folder {folder_path!r} for {title!r}: {e}")

    new_id = db.add_series(
        komga_series_id,
        title=title,
        publisher=publisher,
        year_began=year_began,
        folder_path=folder_path,
        on_pull_list=req.on_pull_list,
        locg_series_id=locg_series_id,
        cv_volume_id=str(req.cv_volume_id) if req.cv_volume_id else None,
        path=DB_PATH,
    )
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg_sync():
        sync_one_guarded(added, _sync_one)
        if req.on_pull_list:
            issues = db.get_issues_for_series(new_id, DB_PATH)
            today_str = str(date.today())
            # Batch the queue insert — per-issue queue_issue is one fresh
            # connection + fsync EACH, which the NAS disk does not appreciate.
            pairs = [(new_id, issue["number"]) for issue in issues
                     if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today_str)]
            if pairs:
                db.queue_issues_bulk(pairs, DB_PATH)
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
        # A one-shot LOCG returned at the issue level (its id is a COMIC id, not a
        # series id) — the wizard resolves it up to its series on pick before add.
        **({"needs_resolve": True, "slug": r["slug"]} if r.get("comic") else {}),
    } for r in raw[:15]]


@app.get("/api/search/locg/resolve")
def resolve_locg_comic(comic_id: int, slug: str):
    """A one-shot search hit (/comic/{id}) -> its parent series id, so it can be added
    as a normal tracked series. Called lazily by the wizard when such a result is
    picked — never on the type-ahead path, so it costs one fetch per add, not per key."""
    locg_client = _locg()
    sid = (locg_client.resolve_comic_series(comic_id, slug) if locg_client
           else _locg_resolve_comic_anon(comic_id, slug))
    if not sid:
        raise HTTPException(404, "This one-shot isn't linked to a series on LOCG")
    return {"series_id": sid}


@app.get("/api/search/storyline")
def search_storyline(q: str):
    """Arc-first entry point: search a storyline, get it back resolved to the RUN it
    originates in (the run you'd follow). Shaped to the wizard's result language
    (kind/source/series/publisher) so it renders and Follows like any other hit.
    Only storylines whose origin run resolves are returned — you can't follow what we
    can't anchor. Empty list if CV isn't configured."""
    cv = _comicvine()
    if not cv:
        return []
    out = []
    for s in cv.search_storylines(q):
        if not s.get("origin_volume_id"):
            continue
        out.append({
            "kind": "storyline",
            "source": "storyline",
            "series": s["name"],                       # the storyline's own name
            "cv_arc_id": s["cv_arc_id"],
            "origin_title": s["origin_title"],
            "origin_year": s["origin_year"],
            "origin_publisher": s.get("origin_publisher"),
            "origin_volume_id": s["origin_volume_id"],
            "publisher": {"name": s["origin_publisher"]} if s.get("origin_publisher") else None,
        })
    return out


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
    # daemon=True or a container stop hangs waiting on a mid-flight sweep — the
    # last_full_sync stamp lands at the END anyway, so a killed run just reads
    # as "missed" and the startup catch-up retries it. Nothing is lost.
    threading.Thread(target=_sync_all_job, daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/sync/{series_id}")
def sync_one(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    threading.Thread(target=sync_one_guarded, args=(s, _sync_one), daemon=True).start()
    return {"ok": True}


# --- pull list ---

@app.get("/api/pull-list")
def pull_list(days: int = 90, past: int = 0):
    return db.get_upcoming_issues(days, past, DB_PATH)


# --- thumbnails ---

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
    # One transaction for the whole batch — queue_issue fsyncs per call, and a
    # long-running series can shove dozens of issues through here at once.
    pairs = [(series_id, issue["number"]) for issue in issues
             if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today)]
    queued = db.queue_issues_bulk(pairs, DB_PATH) if pairs else 0
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
