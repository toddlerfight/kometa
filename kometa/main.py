import os
import json
import re
import logging
import threading
from datetime import date, datetime, timezone
from contextlib import asynccontextmanager


from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.locg_client import search_series_anon as _locg_search_anon, get_issue_details_anon as _locg_issue_details, resolve_comic_series_anon as _locg_resolve_comic_anon, full_series_title_anon as _locg_full_title, is_truncated as _locg_truncated
from kometa.models import AddSeriesRequest
from kometa.arcs import (
    router as _arcs_router, _owned_collection, _add_arc,
)
from kometa.scheduler import start_scheduler, last_scheduled_sync_utc
from kometa.version import build_info as _build_info
import kometa.db as db
from kometa.sources import (
    comics_root as _comics_root, comicvine as _comicvine,
)
from kometa.naming import (
    find_issue_file as _find_issue_file, _resolve_dir,
)
from kometa.sync import (
    sync_one as _sync_one, sync_one_guarded, full_sync_lock,
    rescan_owned as _rescan_owned,
    enrich_trades as _enrich_trades,
)
from kometa.acquisition import (
    get_progress, get_search_status,
    _process_queue, _sweep_missing,
    _poll_usenet_jobs, _poll_torrent_jobs, _release_day_retry,
)

logger = logging.getLogger(__name__)

# Without this, app loggers fall through to Python's last-resort handler and
# only WARNING+ ever reaches `docker logs` — every INFO breadcrumb (sync steps,
# cascade decisions, guarded-exception context) silently vanishes. We debugged
# blind for months because of that. KOMETA_LOG_LEVEL overrides (e.g. DEBUG).
logging.basicConfig(
    level=os.environ.get("KOMETA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# APScheduler narrates every 5-second poller tick at INFO — that's not
# observability, that's a firehose aimed at the one log we just unclogged.
logging.getLogger("apscheduler").setLevel(logging.WARNING)

DB_PATH = db.DB_PATH

# Series NOT on the pull list get a weekly check, not three a day: ~900 series
# x 3 syncs is ~2,700 LOCG lookups a day for runs that mostly never change, and a
# rate-limited LOCG would slow the pull list too. Spread over the week — each
# full sync takes at most this many of the stalest — so no single run spikes.
NOT_PULLED_SYNC_DAYS = 7
NOT_PULLED_PER_RUN = 60


def _series_due_for_sync(all_series: list[dict]) -> list[dict]:
    """Pull-list series every run (as before); the rest once they're a week old,
    stalest first, capped per run."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NOT_PULLED_SYNC_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    pulled = [s for s in all_series if s.get("on_pull_list") or s.get("kind") == "arc"]
    stale = sorted((s for s in all_series
                    if not (s.get("on_pull_list") or s.get("kind") == "arc")
                    and (s.get("last_synced") or "") < cutoff),
                   key=lambda s: s.get("last_synced") or "")
    return pulled + stale[:NOT_PULLED_PER_RUN]


def _sync_all_job():
    # DEAD MOUNT CHECK. /comics rides an SMB share now, and a container that
    # wins the post-reboot race against macOS's mounter sees a perfectly
    # healthy, perfectly EMPTY directory where 900 series used to live. Sync
    # against that void and every folder scan reads as "gone" — states flip,
    # the queue fills with downloads for books sitting right there on the NAS.
    # An empty comics root is not a library; it's a mount that hasn't shown up
    # to work yet. Refuse the whole job and let the next cron slot try again.
    root = _comics_root()
    try:
        populated = any(os.scandir(root))
    except OSError:
        populated = False
    if not populated:
        logger.error(f"Comics root {root} is missing or empty — mount not up? Refusing to sync/sweep.")
        return
    # One full sweep at a time. A deploy near a cron hour fires BOTH the startup
    # catch-up and the scheduler; without this they interleave 47 series' worth
    # of SQLite writes and someone hits "database is locked".
    if not full_sync_lock.acquire(blocking=False):
        logger.info("Full sync already running — skipping this invocation")
        return
    try:
        for s in _series_due_for_sync(db.get_all_series(DB_PATH)):
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
        # The reader's whole-shelf index rides the same schedule: new folders and
        # files (and tracked/untracked links) refresh three times a day.
        from kometa.shelf import scan_shelf_safe
        scan_shelf_safe()
    finally:
        full_sync_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    # folder paths spelled the old share's way (an accent as 'e' + mark) point at
    # the folder as NFS spells it; a duplicate the shelf made of it folds back
    try:
        for line in db.heal_folder_spellings(DB_PATH):
            logger.info(f"Folder spelling: {line}")
    except Exception as e:
        logger.warning(f"Folder spelling repair failed: {e}")
    # Recover items orphaned by a mid-flight container restart
    db.reset_stuck_queue_items(DB_PATH)
    from kometa.shelf_import import trickle_tick
    scheduler = start_scheduler(_sync_all_job, _process_queue, _release_day_retry, _poll_usenet_jobs, _poll_torrent_jobs,
                                trickle_fn=trickle_tick)
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
    _shelf_scan(skip_if_fresh=True)   # whole-shelf index for the reader — skipped if one ran lately
    yield
    # Stop cleanly. The scheduler was never shut down: its job threads aren't
    # daemons, so a stop waited for whatever was running (a sync, a placement, a
    # trickle yielding to a reader for minutes) until Docker killed the process —
    # the stuck restarts. Nothing new starts; waiting loops wake and give up.
    from kometa.activity import STOPPING
    STOPPING.set()
    try:
        scheduler.shutdown(wait=False)
    except Exception as e:
        logger.warning(f"Scheduler shutdown: {e}")


app = FastAPI(lifespan=lifespan)

# Compress what's text — JSON, the JS and CSS, OPDS XML: /api/series went out as
# 761 KB raw (65 KB gzipped), app.js 255 KB (72 KB). Covers, pages and comic
# files are already compressed; gzipping them again only burns CPU, so they join
# Starlette's exclusion list (checked per response; test_gzip pins this).
import starlette.middleware.gzip as _gzip  # noqa: E402
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402
_gzip.DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream", "image/", "video/", "audio/", "font/",
                                        "application/zip", "application/x-cbz", "application/x-cbr",
                                        "application/vnd.comicbook", "application/octet-stream", "application/pdf")
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


class _RequestWrap:
    """Two jobs, one plain ASGI layer (BaseHTTPMiddleware ran every response —
    each reader page and cover — through extra stream tasks, twice):

    - everything a web request does is interactive (kometa/interactive.py): the
      rate-limited sources fail fast for it instead of queueing behind background
      jobs. Set on this task, so it rides contextvars into the threadpool;
      scheduler jobs and threads a handler starts don't carry it.
    - app.js?v=300, style.css?v=249: the version is in the URL and index.html
      names the current one, so a versioned asset is cached for a year instead of
      revalidated by heuristic on every load."""

    IMMUTABLE = b"public, max-age=31536000, immutable"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from kometa.interactive import INTERACTIVE
        path, qs = scope.get("path", ""), scope.get("query_string", b"")
        versioned = path.endswith((".js", ".css")) and (qs.startswith(b"v=") or b"&v=" in qs)

        async def send_wrapped(message):
            if versioned and message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": headers + [(b"cache-control", self.IMMUTABLE)]}
            await send(message)

        token = INTERACTIVE.set(True)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            INTERACTIVE.reset(token)


app.add_middleware(_RequestWrap)

# Cover-art pipeline (thumbnail routes + disk cache) lives in kometa/thumbnails —
# fully self-contained; nothing else in the app imports from it.
from kometa.thumbnails import router as _thumbnails_router  # noqa: E402
app.include_router(_thumbnails_router)
from kometa.reader import router as _reader_router  # noqa: E402
app.include_router(_reader_router)
from kometa.shelf import router as _shelf_router, scan_in_background as _shelf_scan  # noqa: E402
app.include_router(_shelf_router)
from kometa.ondeck import router as _ondeck_router          # On Deck: where reading happens
app.include_router(_ondeck_router)
from kometa.wantread import router as _wantread_router      # Want to read: a list you build by hand, a row on On Deck
app.include_router(_wantread_router)
from kometa.readlists import router as _readlists_router    # reading lists: CBL import, resolved against the shelf
app.include_router(_readlists_router)
from kometa.listget import router as _listget_router        # 'Get' on a reading list: track without pulling, request what the list needs
app.include_router(_listget_router)
from kometa.proposals import router as _proposals_router    # downloads you confirm before they're placed
app.include_router(_proposals_router)
from kometa.related import router as _related_router        # related to this series / suggestions on On Deck
app.include_router(_related_router)
from kometa.trending import router as _trending_router      # what shops sold most (ICv2)
app.include_router(_trending_router)
from kometa.harvest import router as _harvest_router        # the library harvest, all trickles at a glance
app.include_router(_harvest_router)
from kometa.opds import router as _opds_router              # OPDS 1.2 + PSE for Panels & co
app.include_router(_opds_router)
from kometa.marks import router as _marks_router            # favourites + ratings, next-in-series
app.include_router(_marks_router)
from kometa.tradefill import router as _tradefill_router    # trades fill issues no one shares as singles
app.include_router(_tradefill_router)
from kometa.people import router as _people_router          # search by writer / artist
app.include_router(_people_router)
from kometa.search import router as _search_router          # universal search: series, people, lists, issue jumps
app.include_router(_search_router)
# Story-arc machinery + routes live in kometa/arcs (imported at the top with the
# three functions main's own routes call back into).
app.include_router(_arcs_router)


# --- sync logic ---

def _summary(issues):
    from datetime import timedelta
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    owned = sum(1 for r in issues if r["owned"])
    wanted = [r for r in issues if not r["owned"] and not r.get("ignored")]
    # an issue a trade on the shelf already holds isn't missing — the Library card's
    # SQL (db.get_all_series_summaries) has always said so; the series page didn't
    missing = sum(1 for r in wanted if (not r["store_date"] or r["store_date"] < today) and not r.get("covered_by"))
    upcoming = sum(1 for r in wanted if r["store_date"] and r["store_date"] >= today)
    soon = [r["store_date"] for r in wanted
            if r["store_date"] and today <= r["store_date"] <= cutoff]
    week_ago = str(date.today() - timedelta(days=db.RECENT_RELEASE_DAYS))
    recent = [r["store_date"] for r in issues
              if not r.get("ignored") and r["store_date"] and week_ago <= r["store_date"] <= today]
    next_release = min(soon) if soon else None
    return {"owned": owned, "missing": missing, "upcoming": upcoming,
            "next_release": next_release,
            "calendar_date": db.calendar_date(max(recent) if recent else None, next_release)}


# --- connection tests ---

# Test endpoints accept creds in-body but fall back to STORED config — secrets
# never round-trip through the browser, so 'Test' must work against what's saved.
def _stored(key: str) -> str:
    return db.get_config(DB_PATH).get(key, "")


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


class TestComicVineRequest(BaseModel):
    apikey: str | None = None


class TestGcdRequest(BaseModel):
    user: str | None = None
    password: str | None = None


@app.post("/api/test/metron")
def test_metron():
    from kometa import metron_client
    ok, detail = metron_client.test()
    return {"ok": True, "detail": detail} if ok else {"ok": False, "error": detail}


@app.post("/api/test/gcd")
def test_gcd(req: TestGcdRequest):
    from kometa import gcd_client
    ok, detail = gcd_client.test(req.user, req.password)
    return {"ok": True, "detail": detail} if ok else {"ok": False, "error": detail}


@app.post("/api/test/comicvine")
def test_comicvine(req: TestComicVineRequest):
    apikey = req.apikey or _stored("cv_api_key")
    if not apikey:
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.comicvine_client import ComicVineClient
        ok, detail = ComicVineClient(apikey).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": detail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Integration → config keys it owns. Drives 'Disconnect': blank password fields
# mean 'keep current', so without this there is NO path to remove a credential.
_INTEGRATION_KEYS = {
    "sabnzbd":   ["sab_url", "sab_apikey"],
    "qbit":      ["qbit_url", "qbit_user", "qbit_pass"],
    "prowlarr":  ["prowlarr_url", "prowlarr_apikey"],
    "comicvine": ["cv_api_key"],
    "gcd":       ["gcd_user", "gcd_pass"],
    # the refusal mark is deliberately NOT cleared: it's the ban guard, not a credential
    "metron":    ["metron_token", "metron_user", "metron_pass"],
}


@app.post("/api/config/disconnect/{integration}")
def disconnect_integration(integration: str):
    keys = _INTEGRATION_KEYS.get(integration) or _EXTRA_INTEGRATION_KEYS.get(integration)
    if not keys:
        raise HTTPException(404)
    db.set_config({k: "" for k in keys}, DB_PATH)
    if integration == "locg":
        from kometa.locg_client import _access_cache
        _access_cache["ts"] = 0.0
    return get_config()


_EXTRA_INTEGRATION_KEYS = {"locg": ["locg_cf_clearance", "locg_user_agent", "locg_cf_set_at", "locg_impersonate"]}


def _backup_last() -> dict:
    try:
        from kometa import backup
        return backup.last(DB_PATH)
    except Exception:
        return {}


def _locg_status() -> dict:
    """Pass live / paused until / what the top-up queue holds (kometa/topup.py)."""
    try:
        from kometa import topup
        return topup.status(DB_PATH)
    except Exception:
        return {"locg_pass_live": False, "locg_paused_until": None, "locg_topup_waiting": 0, "locg_topup_by_kind": {}}


def settings_attention() -> list[dict]:
    """Everything in Settings that's waiting on YOU — the nav badge's count and
    each card's red dot. Only states you can fix from Settings; a source that's
    switched off never nags."""
    cfg = db.get_config(DB_PATH)
    on = lambda k: cfg.get(k, "1") != "0"
    has = lambda *keys: all((cfg.get(k) or "").strip() for k in keys)
    out = []
    root = _comics_root()
    if not (os.path.isdir(root) and os.access(root, os.W_OK)):
        out.append({"card": "root", "reason": "comics folder missing or read-only"})
    if on("metron_enabled"):
        from kometa import metron_client
        creds = metron_client._creds()
        if not creds:
            out.append({"card": "metron", "reason": "no API token"})
        elif metron_client._refused(creds):
            out.append({"card": "metron", "reason": "Metron refused the token"})
    if on("comicvine_enabled") and not has("cv_api_key"):
        out.append({"card": "comicvine", "reason": "no API key"})
    if on("gcd_enabled") and not has("gcd_user", "gcd_pass"):
        out.append({"card": "gcd", "reason": "no login"})
    if on("locg_enabled"):
        st = _locg_status()
        waiting = (st.get("locg_topup_waiting") or 0) + (st.get("locg_sweep_pending") or 0)
        if waiting and not st.get("locg_pass_live"):
            out.append({"card": "locg", "reason": f"{waiting} waiting on a fresh pass"})
    if on("prowlarr_enabled"):
        if not has("prowlarr_url", "prowlarr_apikey"):
            out.append({"card": "prowlarr", "reason": "not set up"})
        if on("usenet_enabled") and not has("sab_url", "sab_apikey"):
            out.append({"card": "sabnzbd", "reason": "not set up"})
        if on("torrent_enabled") and not has("qbit_url", "qbit_user"):
            out.append({"card": "qbit", "reason": "not set up"})
    return out


@app.get("/api/settings/attention")
def get_settings_attention():
    return settings_attention()


@app.get("/api/backup")
def backup_status():
    from kometa import backup
    return backup.status()


@app.post("/api/backup")
def backup_run():
    """Run the nightly backup now, in a thread."""
    from kometa import backup
    return {"ok": True, "started": backup.run_in_background(), **backup.status()}


@app.post("/api/locg/topup")
def locg_topup_now():
    """'Top up now': drain the LOCG queue in a thread while the pass is live."""
    from kometa import topup, locg_client
    if locg_client.locg_paused() is not None:
        return {"ok": False, "started": False, "error": "LOCG is paused", **topup.status(DB_PATH)}
    started = topup.drain_in_background()
    return {"ok": True, "started": started, **topup.status(DB_PATH)}


@app.post("/api/test/locg")
def test_locg_access():
    """Settings 'test' for the LOCG card. With a pass pasted: try every handshake
    Kometa can wear, remember the one LOCG accepts (config locg_impersonate),
    lift the pause. None accepted → the pass is forgotten, and you're told which
    handshakes were refused. No pass: the same single knock as 'Test LOCG now'."""
    from kometa import locg_client as lc
    if lc._access()[0]:
        results = lc.probe_matrix()
        hit = next((r for r in results if r["status"] == 200), None)
        if hit:
            db.set_config({"locg_impersonate": hit["target"], "locg_paused_until": "0"}, DB_PATH)
            lc._pause["until"] = 0.0
            lc._access_cache["ts"] = 0.0
            # the paste moment: drain what waited for a pass, in the background
            from kometa import topup
            topup.drain_in_background()
            return {"ok": True, "detail": f"LOCG accepted your pass with the {hit['target']} handshake — pause lifted"}
        lc._forget_access()
        tried = ", ".join(f"{r['target']} {r['status'] or 'err'}" for r in results)
        return {"ok": False, "error": f"LOCG refused the pass with every handshake ({tried})"}
    r = lc.probe()
    return {"ok": True, "detail": r["detail"]} if r["ok"] else {"ok": False, "error": r["detail"]}


# --- version ---

@app.get("/api/version")
def get_version():
    """What commit is this, and is the running process actually it?

    The honest answer to "did my deploy land", which nothing else here could
    give you.
    """
    return _build_info()


# --- config ---

@app.get("/api/config")
def get_config():
    cfg = db.get_config(DB_PATH)
    root = _comics_root()
    return {
        "comics_root":         root,
        # Usable = exists and writable. Drives the just-in-time folder prompt:
        # a properly-mounted deploy is ok and never gets nagged.
        "comics_root_ok":      os.path.isdir(root) and os.access(root, os.W_OK),
        "sync_hours":          cfg.get("sync_hours", "5,12,17"),
        "sab_url":             cfg.get("sab_url", ""),
        "sab_configured":      bool(cfg.get("sab_url", "") and cfg.get("sab_apikey", "")),
        "qbit_url":            cfg.get("qbit_url", ""),
        "qbit_user":           cfg.get("qbit_user", ""),
        "qbit_pass":           "",
        "qbit_configured":     bool(cfg.get("qbit_url", "") and cfg.get("qbit_user", "")),
        "prowlarr_url":        cfg.get("prowlarr_url", ""),
        "prowlarr_apikey":     "",
        "prowlarr_configured": bool(cfg.get("prowlarr_url", "") and cfg.get("prowlarr_apikey", "")),
        "cv_api_key":          "",
        "comicvine_configured": bool(cfg.get("cv_api_key", "")),
        # Integration/search toggles — absent = enabled (existing installs unchanged).
        "backup_last":         _backup_last(),
        # Prowlarr is the master search switch; usenet/torrent are its children.
        "prowlarr_enabled":    cfg.get("prowlarr_enabled", "1") != "0",
        "usenet_enabled":      cfg.get("usenet_enabled", "1") != "0",
        "torrent_enabled":     cfg.get("torrent_enabled", "1") != "0",
        "comicvine_enabled":   cfg.get("comicvine_enabled", "1") != "0",
        # Grand Comics Database: the back catalogue (kometa/gcd_client.py). Password never echoed.
        "gcd_user":            cfg.get("gcd_user", ""),
        "gcd_pass":            "",
        "gcd_configured":      bool(cfg.get("gcd_user", "") and cfg.get("gcd_pass", "")),
        "gcd_enabled":         cfg.get("gcd_enabled", "1") != "0",
        # Metron: the primary metadata source (docs/reader-spec.md, 2026-10-08)
        "metron_user":         cfg.get("metron_user", ""),
        "metron_pass":         "",
        "metron_token":        "",
        "metron_token_set":    bool((cfg.get("metron_token") or "").strip()),
        "metron_configured":   bool((cfg.get("metron_token") or "").strip() or (cfg.get("metron_user", "") and cfg.get("metron_pass", ""))),
        "metron_enabled":      cfg.get("metron_enabled", "1") != "0",
        # LOCG: your own browser's pass (kometa/locg_client.py). Never echoed back.
        "locg_cf_configured":  bool(cfg.get("locg_cf_clearance", "")),
        "locg_enabled":        cfg.get("locg_enabled", "1") != "0",
        "locg_cf_set_at":      cfg.get("locg_cf_set_at", ""),
        "locg_user_agent":     cfg.get("locg_user_agent", ""),
        **_locg_status(),
    }


class ConfigRequest(BaseModel):
    comics_root:        str | None = None
    sync_hours:         str | None = None
    sab_url:            str | None = None
    sab_apikey:         str | None = None
    qbit_url:           str | None = None
    qbit_user:          str | None = None
    qbit_pass:          str | None = None
    prowlarr_url:       str | None = None
    prowlarr_apikey:    str | None = None
    cv_api_key:         str | None = None
    prowlarr_enabled:   str | None = None   # "1"/"0" — master search toggle
    usenet_enabled:     str | None = None   # "1"/"0" — search-source toggle
    torrent_enabled:    str | None = None
    comicvine_enabled:  str | None = None   # "1"/"0" — metadata source toggle
    gcd_user:           str | None = None
    gcd_pass:           str | None = None
    gcd_enabled:        str | None = None   # "1"/"0" — metadata source toggle
    metron_user:        str | None = None
    metron_pass:        str | None = None
    metron_token:       str | None = None   # API token (preferred over user/pass)
    metron_enabled:     str | None = None   # "1"/"0" — primary metadata source
    locg_enabled:       str | None = None   # "1"/"0" — off = not one request to LOCG
    locg_cf_clearance:  str | None = None   # your browser's Cloudflare pass (expires)
    locg_user_agent:    str | None = None   # the exact UA that pass was issued to


@app.patch("/api/config")
def update_config(req: ConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None and v != ""}
    if "locg_cf_clearance" in updates:
        updates["locg_cf_clearance"] = updates["locg_cf_clearance"].strip().removeprefix("cf_clearance=")
        updates["locg_cf_set_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.set_config(updates, DB_PATH)
    if "locg_cf_clearance" in updates:
        # the Settings field re-verifies on save (POST /api/test/locg) — one knock,
        # and if the pass works the pause lifts right there
        from kometa.locg_client import _access_cache
        _access_cache["ts"] = 0.0
    return get_config()


# Newznab indexers are gone — Prowlarr aggregates every indexer now, so the
# manual per-feed list (and its add/remove endpoints) is retired. Usenet +
# torrent both search through Prowlarr; SAB/qBit just catch the grabs.


# --- series routes ---

@app.get("/api/series")
def list_series():
    # Arcs are a navigation lens, not library items — they're reached via a
    # series' Arcs tab + their own detail page, not as cards in the grid.
    series = [s for s in db.get_all_series(DB_PATH) if s.get("kind") != "arc"]
    summaries = db.get_all_series_summaries(DB_PATH)
    empty = {"owned": 0, "missing": 0, "upcoming": 0, "next_release": None,
             "calendar_date": None, "out_today": 0, "card_image": None}
    from kometa.marks import marks_for, rated_series
    from kometa.family import attach_families, attach_franchises
    fav = marks_for("series", DB_PATH)
    rated = rated_series(DB_PATH)          # the series' own stars, else the mean of its rated issues
    newest = _newest_file_at()
    rows = [dict(s, **summaries.get(s["id"], empty), favourite=fav.get(s["id"], {}).get("favourite", False),
                 rating=rated.get(s["id"]), newest_file_at=newest.get(s["id"])) for s in series]
    rows = attach_franchises(attach_families(rows))
    for r in rows:                     # bookkeeping no screen reads: ~23% of the payload
        for k in _SERIES_LIST_UNUSED:
            r.pop(k, None)
    return rows


_SERIES_LIST_UNUSED = ("metron_fetched_at", "komga_series_id", "added_at", "metron_checked_at",
                       "monitor_status", "locg_fetched_at", "metron_link", "locg_link")


def _newest_file_at() -> dict[int, str]:
    """series id → ISO time of its newest file (the Library's 'Added' sort). One query."""
    from datetime import datetime, timezone
    try:
        with db._connect(DB_PATH) as conn:
            return {r[0]: datetime.fromtimestamp(r[1], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    for r in conn.execute("SELECT tracked_series_id, MAX(mtime) FROM books WHERE tracked_series_id IS NOT NULL AND mtime IS NOT NULL GROUP BY tracked_series_id")}
    except Exception:
        return {}


def _family_for(series_id: int) -> dict:
    """The series' family for its page: the parent (if it is a special) and its
    specials (if it is a run), as light rows the cards can draw."""
    # what families need — series and their counts — not the whole Library build
    # (marks, ratings, newest files, franchises) the page threw away
    from kometa.family import attach_families
    try:
        summaries = db.get_all_series_summaries(DB_PATH)
        rows = [dict(s, **summaries.get(s["id"], {})) for s in db.get_all_series(DB_PATH) if s.get("kind") != "arc"]
        rows = {r["id"]: r for r in attach_families(rows)}
    except Exception:
        return {"family_parent": None, "family_children": []}
    me = rows.get(series_id)
    if not me:
        return {"family_parent": None, "family_children": []}
    light = lambda r: {"id": r["id"], "title": r["title"], "owned": r.get("owned") or 0,
                       "total": (r.get("owned") or 0) + (r.get("missing") or 0), "metron_type": r.get("metron_type"),
                       "calendar_date": r.get("calendar_date"), "publisher": r.get("publisher")}
    parent = rows.get(me.get("family_parent")) if me.get("family_parent") else None
    return {"family_parent": light(parent) if parent else None,
            "family_children": [light(rows[c]) for c in me.get("family_children") or [] if c in rows]}


def _cached_trades(series: dict) -> list[dict] | None:
    """Read the enriched trade cache (owned + file stamped at sync time).
    Self-heals a pre-enrichment cache once, so reads stay fold-scan-free after."""
    cached = db.get_trades(series["id"], DB_PATH)
    if not cached:
        return None
    trades = cached["trades"]
    if trades and ("owned" not in trades[0] or "file" not in trades[0]):
        _enrich_trades(series, trades)
        db.set_trades(series["id"], trades, DB_PATH)
    return _with_book_ids(series, trades)


def _with_book_ids(series: dict, trades: list[dict]) -> list[dict]:
    """An owned trade is a book in the reader: hand its id back with the tile."""
    folder = series.get("folder_path")
    for t in trades:
        b = db.get_book_by_path(os.path.join(folder, t["file"]), DB_PATH) if folder and t.get("file") else None
        t["book_id"] = b["id"] if b else None
    return trades


def _series_marks(series_id: int) -> dict:
    from kometa.marks import series_rating
    try:
        return series_rating(series_id, DB_PATH)
    except Exception:
        return {"favourite": False, "rating": None, "rating_derived": None, "rated_issues": 0}


@app.get("/api/series/{series_id}/status")
def series_status(series_id: int):
    """What the sync pollers watch — last sync and the counts — without the full
    page payload (issues, trades, family, marks) they fetched every 2–3 s."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    summ = db.get_all_series_summaries(DB_PATH, series_id=series_id).get(series_id, {})
    with db._connect(DB_PATH) as conn:
        n = conn.execute("SELECT COUNT(*) FROM issue_status WHERE tracked_series_id = ?", (series_id,)).fetchone()[0]
    return {"id": series_id, "last_synced": s.get("last_synced"), "issue_count": n,
            **{k: summ.get(k) or 0 for k in ("owned", "missing", "upcoming")},
            **{k: summ.get(k) for k in ("next_release", "calendar_date")}}


@app.get("/api/series/{series_id}")
def get_series(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    if s.get("kind") == "arc":
        # An arc's cross-title reading order lives in arc_issues, not issue_status.
        arc_issues = db.get_arc_reading_order(series_id, DB_PATH)
        # Each row's OWN home series id — an arc spans titles, so the frontend needs
        # this to open the REAL issue modal (showIssueModal) scoped to the run that
        # issue actually belongs to, not the arc's own pseudo-series id. Same
        # cv_volume_id -> tracked_series resolution fulfill_arc already does.
        vids = {r.get("cv_volume_id") for r in arc_issues if r.get("cv_volume_id")}
        vid_map = {vid: db.get_series_by_cv_volume(vid, DB_PATH) for vid in vids}
        for r in arc_issues:
            run = vid_map.get(r.get("cv_volume_id"))
            r["tracked_series_id"] = run["id"] if run else None
        owned = sum(1 for i in arc_issues if i["owned"])
        # 'collected' = a trade that collects this arc is ACTUALLY OWNED ON DISK. Disk
        # is the only ownership source — a Komga metadata entry named after the arc does
        # NOT count (that was the old lie: a "collected" arc whose trade had no files).
        coll = _owned_collection(s["title"])
        collection = {"name": coll["title"], "series_id": coll["id"]} if coll else None
        return dict(s, arc_issues=arc_issues, issues=[],
                    owned=owned, missing=len(arc_issues) - owned, upcoming=0,
                    collected=bool(coll), collection=collection,
                    has_trades=False, trade_count=None)
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
    for i in issues:                       # Komga-era fields no screen reads (Komga is retired)
        i.pop("komga_book_id", None)
    return dict(s, issues=issues, trade_count=trade_count, has_trades=has_trades, **_series_marks(series_id), **_family_for(series_id),
                arc_count=arc_count, shelf_id=db.shelf_id_for_series(series_id, DB_PATH),
                **_summary(issues))






























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
    if not locg_id and not s.get("metron_series_id"):
        # Metron-matched, no LOCG id yet: the trickle would get there one series
        # per tick, but you're looking at the tab NOW. One confident LOCG search
        # here (two requests), only while LOCG is open to us.
        from kometa.locg_client import locg_paused, LocgPaused
        from kometa.shelf_import import find_confident_match
        if locg_paused():
            return {"trades": [], "reason": "locg_paused"}
        if s.get("locg_link") == "none":
            return {"trades": [], "reason": "no_locg_match"}
        try:
            locg_id = find_confident_match(s)
        except LocgPaused:
            return {"trades": [], "reason": "locg_paused"}
        except Exception as e:
            logger.info(f"Trades: LOCG link lookup failed for {s['title']!r}: {e}")
            return {"trades": [], "reason": "locg_paused"}
        db.set_locg_link(series_id, "linked" if locg_id else "none", DB_PATH)
        if not locg_id:
            return {"trades": [], "reason": "no_locg_match"}
        db.set_locg_series_id(series_id, locg_id, DB_PATH)
        s = db.get_series_by_id(series_id, DB_PATH)
    # Read the cache sync populated; only hit LOCG live when it's missing or stale
    # (>24h). Trades drift slowly — new editions get solicited over weeks, not hours.
    cached = db.get_trades(series_id, DB_PATH)
    if cached and cached["age"] < 24 * 3600:
        return {"trades": _cached_trades(s), "cached": True}
    # Stale or missing: rebuilt in the background (~25 Metron calls plus LOCG —
    # over a minute inside the request before). A stale list shows now; a missing
    # one says pending and the tab asks again.
    _refresh_trades_bg(s)
    if cached:
        return {"trades": _cached_trades(s), "cached": True, "refreshing": True}
    return {"trades": [], "pending": True}


_trades_refreshing: set = set()
_trades_refreshing_lock = threading.Lock()


def _refresh_trades_bg(s: dict) -> None:
    with _trades_refreshing_lock:
        if s["id"] in _trades_refreshing:
            return
        _trades_refreshing.add(s["id"])

    def run():
        from kometa.record import fill_trades
        try:
            fill_trades(s, force=True)
        except Exception as e:
            logger.info(f"Trades: fill failed for {s['title']!r}: {e}")
            if not db.get_trades(s["id"], DB_PATH):
                db.set_trades(s["id"], [], DB_PATH)        # an answer, even if empty: the tab stops waiting
        finally:
            with _trades_refreshing_lock:
                _trades_refreshing.discard(s["id"])
    threading.Thread(target=run, name=f"trades-{s['id']}", daemon=True).start()


@app.get("/api/trade/{locg_id}/details")
def get_trade_details(locg_id: str):
    """Description + credits for a collected edition — same LOCG page scrape the
    issue modal uses, just addressed by the trade's own comic id."""
    from kometa.record import trade_details
    local = trade_details(locg_id)
    if local is not None:
        return local
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
    shows up in Activity. The file lands in the series folder; issues are not
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

    # One-shot backstop: if the client forwards a comic id + slug, the picked result was
    # an ISSUE, not a series. Resolve it to its parent series HERE, server-side, so a
    # comic id can never be persisted as a series anchor (which yields get_issues == 0
    # → a zombie 'Syncing…' forever). Authoritative: overrides whatever locg_id the
    # client computed. A one-shot that isn't linked to a series is refused, not stored.
    if req.locg_comic_id and req.locg_comic_slug:
        sid = _locg_resolve_comic_anon(req.locg_comic_id, req.locg_comic_slug)
        if not sid:
            raise HTTPException(400, "This one-shot isn't linked to a series on LOCG")
        locg_series_id = sid
        title = re.sub(r"\s*#\s*\d+.*$", "", title).strip()  # drop the '#1' issue suffix

    # An issue-level hit arrives pre-clipped by LOCG ('…Training Day...'), and that
    # stump became the series title AND its folder name — a trailing-dot folder the
    # SMB share can't even show, so the book on disk read as missing forever.
    # Ask LOCG for the series row's real name; failing that, at least lose the dots.
    if locg_series_id and _locg_truncated(title):
        title = _locg_full_title(locg_series_id, title) or re.sub(r"\s*(?:\.{2,}|…)\s*$", "", title)

    # Idempotent add, plain-path edition. The storyline path above dedupes by CV
    # volume; this path had NOTHING — add the same run twice 41 minutes apart and
    # you got twin rows syncing off the same LOCG id (hi, The Foundry ×2). Same
    # LOCG series id = same run, full stop. Return the row we already have.
    if locg_series_id:
        for s in db.get_all_series(DB_PATH):
            if (s.get("kind") != "arc"
                    and str(s.get("locg_series_id") or "") == str(locg_series_id)):
                logger.info(f"Add {title!r}: LOCG series {locg_series_id} already tracked "
                            f"as #{s['id']} — returning existing, not minting a twin")
                return s
    if req.metron_id:
        for s in db.get_all_series(DB_PATH):
            if s.get("kind") != "arc" and s.get("metron_series_id") == req.metron_id:
                logger.info(f"Add {title!r}: Metron series {req.metron_id} already tracked as #{s['id']}")
                return s

    # No folder picked? Derive it from publisher+title. _resolve_dir finds an
    # existing on-disk folder (variation-tolerant) or returns the canonical new
    # path, so the first sync reconciles owned-vs-missing correctly whether or not
    # the series is already on disk. The folder is the only link a series needs.
    if req.metron_id and not publisher:
        # Track from a catalogue card carries no publisher (creator works don't):
        # ask Metron for the run's own, or the folder lands under 'Unknown'.
        try:
            from kometa import metron_client
            publisher = metron_client.series_detail(req.metron_id).get("publisher") or publisher
        except Exception as e:
            logger.info(f"Add {title!r}: Metron publisher lookup skipped: {e}")
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
        title=title,
        publisher=publisher,
        year_began=year_began,
        folder_path=folder_path,
        on_pull_list=req.on_pull_list,
        locg_series_id=locg_series_id,
        cv_volume_id=str(req.cv_volume_id) if req.cv_volume_id else None,
        path=DB_PATH,
    )
    if req.metron_id:
        db.set_metron_series_id(new_id, req.metron_id, DB_PATH)
        db.set_metron_link(new_id, "linked", DB_PATH)
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg_sync():
        # A pick with no catalogue id (a ComicVine volume while LOCG is shut) would
        # sync to an EMPTY page and sit there until the trickle got round to it.
        # Ask Metron by title first, the same confident rule the matcher uses.
        if not (added.get("locg_series_id") or added.get("metron_series_id")):
            try:
                from kometa.shelf_import import find_metron_match
                mid = find_metron_match(added)
                if mid:
                    db.set_metron_series_id(new_id, mid, DB_PATH)
                    db.set_metron_link(new_id, "linked", DB_PATH)
                    added.update(metron_series_id=mid)
                    logger.info(f"Add {title!r}: linked to Metron {mid} by title")
            except Exception as e:
                logger.info(f"Add {title!r}: Metron title match skipped: {e}")
        sync_one_guarded(added, _sync_one)
        if req.on_pull_list:
            issues = db.get_issues_for_series(new_id, DB_PATH)
            today_str = str(date.today())
            # Batch the queue insert — per-issue queue_issue is one fresh
            # connection + fsync EACH, which the NAS disk does not appreciate.
            pairs = [(new_id, issue["number"]) for issue in issues
                     if not issue["owned"] and not issue.get("ignored")
                     and (not issue["store_date"] or issue["store_date"] <= today_str)]
            if pairs:
                db.queue_issues_bulk(pairs, DB_PATH)
            _process_queue()

    threading.Thread(target=_bg_sync, daemon=True).start()
    return added


@app.delete("/api/series/{series_id}")
def delete_series(series_id: int):
    """One Remove, one meaning: same as POST …/trash — the series AND its folder
    (to _trash, 7-day purge, restorable). Kept as DELETE for older callers."""
    from kometa import trash
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    try:
        return trash.trash_series(series_id)
    except trash.TrashError as e:
        raise HTTPException(400, str(e))


class RestoreRequest(BaseModel):
    path: str


@app.post("/api/trash/restore")
def restore_from_trash(req: RestoreRequest):
    from kometa import trash
    from kometa.shelf import scan_in_background
    try:
        origin = trash.restore(req.path)
    except trash.TrashError as e:
        raise HTTPException(400, str(e))
    scan_in_background()                 # the folder is back: it becomes a series again
    return {"restored": origin}


@app.get("/api/series/{series_id}/tidy")
def tidy_plan(series_id: int):
    """Dry run: what tidying this series' files would do (kometa/tidy.py)."""
    from kometa import tidy
    try:
        return tidy.plan(series_id)
    except tidy.TidyError as e:
        raise HTTPException(400, str(e))


class FileUnderRequest(BaseModel):
    parent_id: int


@app.post("/api/series/{series_id}/file-under")
def file_under(series_id: int, req: FileUnderRequest):
    """A collected edition filed as a series goes under the run it collects
    (kometa/tidy.py: files move, book rows follow, the stub series goes)."""
    from kometa import tidy
    try:
        return tidy.file_under(series_id, req.parent_id)
    except tidy.TidyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report/collections")
def collections_report():
    """Collected editions filed as series: what each collects (publisher wiki)
    and the run on your shelf to file it under. Cache-only; kicks the lookup
    job so 'pending' rows fill in (kometa/filing.py)."""
    from kometa import filing
    filing.refresh_in_background()
    return filing.plan()


class CollectionApplyRequest(BaseModel):
    series_id: int
    parent_id: int


@app.post("/api/report/collections/apply")
def collections_apply(req: CollectionApplyRequest):
    from kometa import filing, tidy
    try:
        return filing.apply(req.series_id, req.parent_id)
    except tidy.TidyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report/twins")
def twins_report():
    """Shelf folders that are the same run as a series you have (kometa/twins.py)."""
    from kometa import twins
    return {"rows": twins.find_twins()}


class TwinRequest(BaseModel):
    shelf_id: int
    series_id: int


@app.post("/api/report/twins/plan")
def twins_plan(req: TwinRequest):
    from kometa import twins
    try:
        return twins.plan(req.shelf_id, req.series_id)
    except twins.TwinError as e:
        raise HTTPException(400, str(e))


@app.post("/api/report/twins/apply")
def twins_apply(req: TwinRequest):
    from kometa import twins
    try:
        return twins.apply(req.shelf_id, req.series_id)
    except twins.TwinError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report/combine")
def combine_report():
    """Groups of one-file folders that are one series (kometa/combine.py)."""
    from kometa import combine
    return {"groups": combine.find_groups()}


class CombineRequest(BaseModel):
    ids: list[int]
    title: str | None = None
    order: str | list[int] = "year"


@app.post("/api/report/combine/plan")
def combine_plan(req: CombineRequest):
    from kometa import combine
    try:
        return combine.plan(req.ids, req.title, req.order)
    except combine.CombineError as e:
        raise HTTPException(400, str(e))


class AddToRequest(BaseModel):
    ids: list[int]
    into: int
    number: float | None = None


@app.post("/api/report/combine/add")
def combine_add(req: AddToRequest):
    """One-file series → the next issue(s) of a series you have (kometa/combine.py)."""
    from kometa import combine
    try:
        return combine.add_to(req.ids, req.into, req.number)
    except combine.CombineError as e:
        raise HTTPException(400, str(e))


@app.post("/api/report/combine/apply")
def combine_apply(req: CombineRequest):
    from kometa import combine
    try:
        return combine.apply(req.ids, req.title, req.order)
    except combine.CombineError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report/singles")
def singles_report():
    """Single-file series classified (one-shot / collected / split / unknown).
    Read-only. Kicks the Metron series-type backfill so 'unknown' shrinks."""
    from kometa import singles
    singles.backfill_in_background()
    return singles.report()


@app.post("/api/series/{series_id}/tidy")
def tidy_apply(series_id: int):
    from kometa import tidy
    try:
        return tidy.apply(series_id)
    except tidy.TidyError as e:
        raise HTTPException(400, str(e))


class PullListRequest(BaseModel):
    on_pull_list: bool


@app.patch("/api/series/{series_id}/pull-list", status_code=200)
def toggle_pull_list(series_id: int, req: PullListRequest):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    db.set_pull_list(series_id, req.on_pull_list, DB_PATH)
    # Off means "stop getting this" now, not after whatever's queued.
    cancelled = 0 if req.on_pull_list else db.dequeue_waiting_series(series_id, DB_PATH)
    if req.on_pull_list:
        # On means "go get it" now, not at the next scheduled sweep: refresh the
        # issue list, queue what's missing, start the queue. All in the background.
        def _go():
            sync_one_guarded(db.get_series_by_id(series_id, DB_PATH), lambda x: _sync_one(x, force=True))
            _sweep_missing()
            _process_queue()
        threading.Thread(target=_go, name=f"pull-on-{series_id}", daemon=True).start()
    return {**db.get_series_by_id(series_id, DB_PATH), "cancelled": cancelled, "searching": bool(req.on_pull_list)}


class MatchRequest(BaseModel):
    locg_id: int | None = None
    metron_id: int | None = None
    none: bool = False          # "there is no run": a collection, a fan book, a folder of one-shots


@app.patch("/api/series/{series_id}/locg", status_code=200)
def set_series_locg(series_id: int, req: MatchRequest):
    """You picked the run (on Metron or LOCG): link it, mark it as your choice,
    refresh in the background. Route name predates Metron.

    none=true: you looked and there is no run to pick — an omnibus library,
    a folder of yearly specials, a fan comic. The series leaves Needs matching
    as a shelf-only series (readable, no issue list) and is never asked again."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    if req.none:
        db.set_match_status(series_id, "manual", DB_PATH)
        db.set_metron_link(series_id, "none", DB_PATH)
        db.set_locg_link(series_id, "none", DB_PATH)
        # a person said there is no run: neither the re-match job nor the
        # re-check queue ever asks about it again
        from kometa.rematch import NEVER, ensure_columns
        ensure_columns(DB_PATH)
        with db._connect(DB_PATH) as conn:
            conn.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (NEVER, series_id))
        return db.get_series_by_id(series_id, DB_PATH)
    if not (req.locg_id or req.metron_id):
        raise HTTPException(400, "Pick a run")
    if req.locg_id:
        db.set_locg_series_id(series_id, req.locg_id, DB_PATH)
    if req.metron_id:
        db.set_metron_series_id(series_id, req.metron_id, DB_PATH)
        db.set_metron_link(series_id, "linked", DB_PATH)
    db.set_match_status(series_id, "manual", DB_PATH)
    threading.Thread(target=sync_one_guarded,
                     args=(db.get_series_by_id(series_id, DB_PATH), lambda x: _sync_one(x, force=True)),
                     daemon=True).start()
    return db.get_series_by_id(series_id, DB_PATH)


@app.post("/api/series/{series_id}/match")
def match_series_now(series_id: int):
    """'Match now' — one LOCG lookup for this series, right away."""
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    from kometa.shelf_import import match_one
    from kometa.locg_client import LocgPaused
    try:
        return match_one(series_id)
    except LocgPaused as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"The catalogue didn't answer: {e}")


@app.post("/api/locg/test")
def locg_test():
    """One request to see whether LOCG answers again (ignores Kometa's own pause)."""
    from kometa.locg_client import probe, locg_paused, pause_label
    r = probe()
    until = locg_paused()
    return {**r, "paused_until_label": pause_label(until) if until else None}


@app.get("/api/locg/status")
def locg_status():
    from kometa.locg_client import locg_paused, pause_label
    until = locg_paused()
    return {"paused_until": until, "paused_until_label": pause_label(until) if until else None}


class PageMaxRequest(BaseModel):
    page_max: int | None = None


@app.patch("/api/series/{series_id}/page-max", status_code=200)
def set_page_max(series_id: int, req: PageMaxRequest):
    """Per-series single-issue page ceiling for oversized formats (e.g. Head
    Lopper's 72-page quarterly). None reverts to the global default."""
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    if req.page_max is not None and not (1 <= req.page_max <= 1000):
        raise HTTPException(422, "page_max must be 1-1000 or null")
    db.set_page_max(series_id, req.page_max, DB_PATH)
    return db.get_series_by_id(series_id, DB_PATH)


# --- search ---

@app.get("/api/search/metron")
def search_metron(q: str):
    """Run candidates from Metron for the picker — same row shape as /api/search/locg."""
    from kometa import metron_client
    if not metron_client.configured():
        return []
    try:
        rows = metron_client.search_series(q)
    except metron_client.MetronUnavailable as e:
        raise HTTPException(502, str(e))
    return [{"id": r["id"], "series": r["title"], "publisher": {"name": r["publisher"]},
             "year_began": r["year"], "cover": None, "issue_count": r["issue_count"], "source": "metron"}
            for r in rows]


@app.get("/api/search/locg")
def search_locg(q: str):
    raw = _locg_search_anon(q)
    # Series before issues, THEN slice. LOCG hands back its own order, and a run whose
    # every issue is catalogued individually will bury its own series entry under five
    # '#7'-shaped near-misses — or shove it past the 15 entirely, which is how
    # 'Batman: The Adventures Continue' looked like it wasn't in the catalog at all.
    # A series row is what you can actually follow; an issue row needs a resolve hop.
    # Stable sort, so LOCG's relevance order survives inside each bucket.
    raw = sorted(raw, key=lambda r: bool(r.get("comic")))
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
    sid = _locg_resolve_comic_anon(comic_id, slug)
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
def sync_one(series_id: int, force: bool = False):
    """force=1 = you asked (pull-to-refresh): refetch LOCG regardless. The
    automatic on-view refresh doesn't pass it, so it only asks LOCG for what
    can have changed."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    threading.Thread(target=sync_one_guarded,
                     args=(s, (lambda x: _sync_one(x, force=True)) if force else _sync_one),
                     daemon=True).start()
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

# what no screen reads: indexer URLs with API keys in them (56 KB of the 171 KB
# queue) and the per-row blacklists the acquisition side keeps for itself
_QUEUE_PRIVATE = ("source_url", "failed_sources", "failed_channels", "sab_nzo_id", "torrent_hash", "komga_series_id")


@app.get("/api/queue/summary")
def queue_summary(since: str = ""):
    """The activity badge's three numbers — failed, not found, done since you
    last looked — without the whole queue (it polled all of it every 25 s)."""
    with db._connect(DB_PATH) as conn:
        rows = conn.execute("""SELECT q.state, COUNT(*) AS n FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state IN ('failed', 'not_found', 'done') AND q.updated_at > ? GROUP BY q.state""", (since or "",)).fetchall()
    got = {r["state"]: r["n"] for r in rows}
    return {"failed": got.get("failed", 0), "not_found": got.get("not_found", 0), "done": got.get("done", 0)}


@app.get("/api/queue")
def get_queue():
    items = db.get_queue(DB_PATH)
    # an issue waiting on a trade says what became of the trade: seven rows said
    # 'Getting it in …' for trades that had failed hours before
    trade_fate = {}
    for t in items:
        if t.get("kind") == "trade" and t["state"] in ("failed", "not_found"):
            try:
                meta = json.loads(t.get("meta_json") or "{}")
            except ValueError:
                continue
            fate = "wasn't found" if t["state"] == "not_found" else "failed"
            for qid in meta.get("fills") or []:
                trade_fate[qid] = f"{meta.get('title') or 'The trade'} {fate} — retry it, or drop it to search singles again"
    for item in items:
        if item["state"] == "via_trade" and item["id"] in trade_fate:
            item["error"] = trade_fate[item["id"]]
        item["error"] = db.plain_error(item.get("error"))
        for k in _QUEUE_PRIVATE:
            item.pop(k, None)
        prog = get_progress(item["id"])
        if prog:
            item["progress"] = prog
        if item["state"] in ("searching", "pending_usenet"):     # pending_usenet: 'waiting in SAB' line
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
def retry_queue_item(queue_id: int, fresh: int = 0):
    """'Search now'. If a worker pass is mid-download the ask is remembered and
    runs the moment it's free — and we SAY so, instead of a dead click."""
    from kometa.acquisition import queue_busy
    with db._connect(DB_PATH) as conn:
        row = conn.execute("SELECT kind, torrent_hash, sab_nzo_id FROM download_queue WHERE id = ?", (queue_id,)).fetchone()
    if row and row["kind"] == "list_pack":
        # a pack has nothing to search for ('queued' sent it hunting issue #-1220):
        # back to its download — the poller sees it finished and places it again
        if not (row["torrent_hash"] or row["sab_nzo_id"]):
            raise HTTPException(409, "This pack has no download to go back to — run Get missing on the list")
        db.update_queue_state(queue_id, "pending_torrent" if row["torrent_hash"] else "pending_usenet", error=None, path=DB_PATH)
        return {"ok": True, "busy": False}
    if fresh:
        # forget the releases this row rejected (and the channels it benched):
        # the reason for the rejection was fixed — Where Monsters Dwell #2–4 were
        # turned away by a page check that's since been corrected
        with db._connect(DB_PATH) as conn:
            conn.execute("UPDATE download_queue SET failed_sources = NULL, failed_channels = NULL WHERE id = ?", (queue_id,))
    db.update_queue_state(queue_id, "queued", error=None, path=DB_PATH)
    db.reset_rl_attempts(queue_id, path=DB_PATH)
    busy = queue_busy()
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True, "busy": busy}


@app.post("/api/queue/{queue_id}/try-getcomics", status_code=200)
def try_getcomics(queue_id: int):
    """Cancel the row's usenet/torrent job and go to GetComics now."""
    from kometa.acquisition import try_getcomics_now
    try:
        return try_getcomics_now(queue_id)
    except KeyError:
        raise HTTPException(404, "No such queue item")
    except ValueError as e:
        raise HTTPException(400, str(e))


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
             if not issue["owned"] and not issue.get("ignored")
             and (not issue["store_date"] or issue["store_date"] <= today)]
    queued = db.queue_issues_bulk(pairs, DB_PATH) if pairs else 0
    if queued:
        threading.Thread(target=_process_queue, daemon=True).start()
    return {"queued": queued}


class IgnoreRequest(BaseModel):
    ignored: bool


@app.patch("/api/series/{series_id}/issues/{number}/ignore")
def set_issue_ignored(series_id: int, number: float, req: IgnoreRequest):
    """Stop (or resume) chasing one issue. Ignoring also drops any queue row still
    waiting on it, so a sweep already in flight doesn't grab it anyway."""
    if not db.set_issue_ignored(series_id, number, req.ignored, DB_PATH):
        raise HTTPException(404, "No such issue")
    if req.ignored:
        db.dequeue_waiting_issue(series_id, number, DB_PATH)
    return {"ok": True, "ignored": req.ignored}


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


@app.get("/api/img")
def external_image(u: str):
    """A catalogue card's cover, through Kometa: cached a day on disk (kometa/images.py),
    allow-listed hosts only."""
    from kometa import images
    if not u or not images.ext_allowed(u):
        raise HTTPException(400, "Not an allowed image host")
    resp = images.serve_external(u, DB_PATH)
    if resp is None:
        raise HTTPException(404)
    return resp


@app.get("/api/series/{series_id}/issues/{number}/locg-details")
def get_issue_locg_details(series_id: int, number: float):
    """Description + credits — Metron first, LOCG for issues Metron lacks
    (kometa.issue_meta). Route name kept for the frontend; cached either way
    (the external kometa-recommend project also reads the LOCG entries)."""
    from kometa import record
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue or not (issue.get("locg_issue_id") or issue.get("metron_issue_id")):
        raise HTTPException(404)
    try:
        d = record.details(issue, DB_PATH)        # the local record first; a catalogue only to fill it
    except Exception as e:
        raise HTTPException(502, "Details fetch failed") from e
    return {"desc": d.get("desc", ""), "credits": d.get("credits", []), "source": d.get("source"), "record": d.get("record")}


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
    if not (locg_issue_id or issue.get("metron_issue_id")):
        return {"covers": [], "locg_issue_id": None, "selected_ids": sel_ids, "primary_id": primary}
    from kometa import record
    try:
        data = record.variants(issue, DB_PATH)           # the local record first; a catalogue only to fill it
        return {"covers": data.get("covers", []), "locg_issue_id": locg_issue_id, "source": data.get("source"),
                "selected_ids": sel_ids, "primary_id": primary, "record": data.get("record")}
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
            # inject_covers returns the FINAL path too — a .cbr input comes back
            # as .cbz and the old path is gone from disk.
            added, file_path = inject_covers(file_path, req.selected, req.primary_id)
            # Persist the pick as a display override too, so the card shows YOUR
            # pick at once whatever the cover store holds.
            db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
            # The file's page 1 just changed: re-cut Kometa's own file cover from it.
            try:
                from kometa import covers
                covers.generate_for_path(file_path, series_id, DB_PATH)
            except Exception as e:
                logger.warning(f"File cover refresh failed for {file_path}: {e}")
            return {"ok": True, "added": added}
        except Exception as e:
            raise HTTPException(500, detail=str(e)) from e
    else:
        db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
        return {"ok": True, "queued": True}


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
