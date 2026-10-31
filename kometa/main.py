import os
import time
import json
from starlette.concurrency import run_in_threadpool
import re
import logging
import threading
from datetime import date, datetime, timezone
from contextlib import asynccontextmanager


from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.locg_client import search_series_anon as _locg_search_anon, get_issue_details_anon as _locg_issue_details, resolve_comic_series_anon as _locg_resolve_comic_anon, full_series_title_anon as _locg_full_title, is_truncated as _locg_truncated
from kometa.models import AddSeriesRequest
from kometa.arcs import (
    router as _arcs_router, _owned_collection, _add_arc,
)
from kometa.scheduler import start_scheduler, last_scheduled_sync_utc, next_try
from kometa.version import build_info as _build_info
import kometa.db as db
from kometa import runstatus
from kometa.sources import (
    comics_root as _comics_root, comicvine as _comicvine,
    OutsideLibrary, check_series_folder, check_comics_root,
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
from kometa import scrub as _scrub  # noqa: E402
_scrub.install()                    # no key in docker logs (uvicorn's own handlers join at startup)

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
    # ...and the half-written files their downloads left (before the queue starts)
    from kometa.downloader import clear_partials
    if (n := clear_partials()):
        logger.info(f"Removed {n} half-written download(s) left by the last restart")
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
    from kometa import push as _push
    _push.restore_pending(DB_PATH)    # landings a deploy interrupted still get their one buzz
    _scrub.install("uvicorn", "uvicorn.error", "uvicorn.access")
    from kometa import related_combined as _rc
    _rc.prewarm(DB_PATH)              # the first Related row after a deploy doesn't build the index
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


from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402
from fastapi.exception_handlers import http_exception_handler as _http_exception_handler  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _scrubbed_http_error(request, exc):
    """~40 routes put an exception's text in their error, and a requests error carries
    the URL it tried, key and all: blanked here, once, for all of them."""
    if isinstance(exc.detail, str):
        exc.detail = _scrub.scrub(exc.detail)
    return await _http_exception_handler(request, exc)

# Compress what's text — JSON, the JS and CSS, OPDS XML: /api/series went out as
# 761 KB raw (65 KB gzipped), app.js 255 KB (72 KB). Covers, pages and comic
# files are already compressed; gzipping them again only burns CPU, so they join
# Starlette's exclusion list (checked per response; test_gzip pins this).
import starlette.middleware.gzip as _gzip  # noqa: E402
import warnings  # noqa: E402
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)     # defusedxml still touches cElementTree on import
    import defusedxml  # noqa: E402
    defusedxml.defuse_stdlib()   # ComicInfo/CBL come out of archives strangers made: no entity tricks, no external refs
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
    # Belt and braces for the one surface the internet can reach: no MIME sniffing,
    # no framing, no referrer leaking the LAN address. Nothing here changes how
    # the app itself works; it only closes doors a browser would otherwise leave ajar.
    HARDEN = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
              (b"content-security-policy", b"frame-ancestors 'none'"), (b"referrer-policy", b"same-origin")]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from kometa.interactive import INTERACTIVE
        path, qs = scope.get("path", ""), scope.get("query_string", b"")
        versioned = path.endswith((".js", ".css", ".woff2")) and (qs.startswith(b"v=") or b"&v=" in qs)

        inm = next((v for k, v in scope.get("headers", []) if k.lower() == b"if-none-match"), b"")
        not_modified = False

        async def send_wrapped(message):
            nonlocal not_modified
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if versioned:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"] + [(b"cache-control", self.IMMUTABLE)]
                # A cover the browser already holds (its ETag matches) goes back as
                # 304 with no body: the daily revalidation of ~800 Library tiles used
                # to re-send every one of them in full.
                etag = next((v for k, v in headers if k.lower() == b"etag"), None)
                if inm and etag and message.get("status") == 200 and etag in inm.split(b","):
                    not_modified = True
                    headers = [(k, v) for k, v in headers if k.lower() not in (b"content-length", b"content-type")]
                    message = {**message, "status": 304, "headers": headers + self.HARDEN}
                else:
                    message = {**message, "headers": headers + self.HARDEN}
            elif message["type"] == "http.response.body" and not_modified:
                message = {**message, "body": b""}
            await send(message)

        token = INTERACTIVE.set(True)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            INTERACTIVE.reset(token)


app.add_middleware(_RequestWrap)
# outermost: who's asking, and may they (kometa/auth.py). Off by default — everyone is the admin.
from kometa.auth import AuthGate, router as _auth_router  # noqa: E402
app.add_middleware(AuthGate)


class _RequestGuard:
    """Who may change things, and for which address (security review 2026-10-11, items 4 + 6).

    - DNS rebinding: a page on attacker.example that re-resolves to this box would be
      same-origin with Kometa. So the Host must be one of ours: an IP literal, localhost,
      a one-word LAN name, .local / .localhost / .ts.net, whatever config `allowed_hosts`
      adds, or anything Cloudflare brought (it only ever carries our own hostname).
    - Cross-site changes: a POST/PUT/PATCH/DELETE that names an Origin (or, failing that,
      a Referer) must name this very address, port and all; a page on another port of the
      same box is another site. No Origin at all — YondeB, curl, OPDS readers — is fine:
      a browser always sends one with a cross-site change."""

    SAFE = {"GET", "HEAD", "OPTIONS"}
    SUFFIXES = (".local", ".localhost", ".ts.net")
    _extra = {"at": 0.0, "hosts": frozenset()}

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _split(hostport: str) -> tuple[str, str | None]:
        hp = hostport.strip().lower()
        if hp.startswith("["):                                   # [::1]:6969
            host, _, rest = hp[1:].partition("]")
            return host, rest[1:] if rest.startswith(":") else None
        host, sep, port = hp.rpartition(":") if hp.count(":") == 1 else (hp, "", "")
        return (host, port) if sep else (hp, None)

    @classmethod
    def _extra_hosts(cls) -> frozenset:
        now = time.monotonic()
        if now - cls._extra["at"] > 30:
            raw = db.get_config(DB_PATH).get("allowed_hosts") or ""
            cls._extra.update(at=now, hosts=frozenset(h.strip().lower() for h in raw.split(",") if h.strip()))
        return cls._extra["hosts"]

    @classmethod
    def plain_ok(cls, host: str) -> bool:
        """Ours without asking the config: an IP, localhost, a one-word or tailnet/mDNS name."""
        import ipaddress
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return "." not in host or host.endswith(cls.SUFFIXES)

    @classmethod
    def listed(cls, host: str) -> bool:
        return host in cls._extra_hosts()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from kometa.auth import via_tunnel
        headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not via_tunnel(scope) and b"host" in headers:    # no Host at all is no browser: nothing to rebind
            host, _ = self._split(headers[b"host"])
            if not self.plain_ok(host) and not await run_in_threadpool(self.listed, host):
                return await self._refuse(send, "Not one of Kometa's addresses")
        if scope["method"] not in self.SAFE:
            origin = headers.get(b"origin") or headers.get(b"referer")
            # behind a proxy (Tailscale serve) the address the browser used may arrive as
            # X-Forwarded-Host; a cross-site page can't set that header without a preflight
            ours = [h for h in (headers.get(b"host"), headers.get(b"x-forwarded-host")) if h]
            if origin is not None and not any(self._same(origin, h) for h in ours):
                return await self._refuse(send, "Changes come from Kometa's own pages")
        return await self.app(scope, receive, send)

    @classmethod
    def _same(cls, origin: str, hostport: str) -> bool:
        from urllib.parse import urlsplit
        if origin.strip() == "null":
            return False
        try:
            u = urlsplit(origin)
            port = str(u.port) if u.port else None
        except ValueError:
            return False
        if not u.hostname:
            return False
        want_host, want_port = cls._split(hostport)
        return u.hostname.lower() == want_host and (port == want_port or None in (port, want_port))

    @staticmethod
    async def _refuse(send, why: str):
        body = json.dumps({"detail": why}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_RequestGuard)     # outermost: a foreign address or a cross-site change never reaches the rest
app.include_router(_auth_router)

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
from kometa.listcatalog import router as _listcatalog_router  # the community's .cbl lists (DieselTech), searchable, added as the reader's own
app.include_router(_listcatalog_router)
from kometa.proposals import router as _proposals_router    # downloads you confirm before they're placed
app.include_router(_proposals_router)
from kometa.related import router as _related_router        # related to this series / suggestions on On Deck
app.include_router(_related_router)
from kometa.trending import router as _trending_router      # what shops sold most (ICv2)
app.include_router(_trending_router)
from kometa.toprated import router as _toprated_router      # community scores already stored, ranked: series ★ sort + Deck picks
app.include_router(_toprated_router)
from kometa.harvest import router as _harvest_router        # the library harvest, all trickles at a glance
app.include_router(_harvest_router)
from kometa.coververify import router as _coververify_router  # which issue is this file, really? ask the cover
app.include_router(_coververify_router)
from kometa.cbr_people import router as _cbr_people_router  # readers like you: CBR's critics + readers, and what they rate
app.include_router(_cbr_people_router)
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
from kometa.retag import router as _retag_router            # read-only: the ComicInfo a retag would write
app.include_router(_retag_router)
from kometa.retag_batch import router as _retag_batch_router  # the full rewrite, phase by phase, armed by hand
app.include_router(_retag_batch_router)
from kometa.push import router as _push_router              # 'it landed' on your phone (APNs → YondeB)
app.include_router(_push_router)
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
    # Issue-based (2026-10-15): a single a trade reprints is still missing until you
    # own the single. Matches db.get_all_series_summaries and the acquisition sweep.
    missing = sum(1 for r in wanted if (not r["store_date"] or r["store_date"] <= today))
    upcoming = sum(1 for r in wanted if r["store_date"] and r["store_date"] > today)
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


def _same_address(a: str | None, b: str | None) -> bool:
    norm = lambda u: (u or "").strip().rstrip("/").lower()
    return norm(a) == norm(b)


def _for_address(req_url: str | None, url_key: str, req_secret: str | None, secret_key: str) -> tuple[str, str, str | None]:
    """(url, secret, why not): the saved secret only ever goes to the saved address. The
    2026-10-11 review: a test naming another server, key left blank, sent it the saved key."""
    saved = _stored(url_key)
    url = req_url or saved
    if req_secret:
        return url, req_secret, None
    if _same_address(url, saved):
        return url, _stored(secret_key), None
    return url, "", "Enter the key for this address — the saved one only goes to the saved address"


def _redact(msg, *secrets: str | None) -> str:
    """An error from requests repeats the URL it tried, api key and all. Not in our answers."""
    out = str(msg)
    for s in secrets:
        if s and len(s) > 3:
            out = out.replace(s, "***")
    return _scrub.scrub(out)


class TestSabRequest(BaseModel):
    url: str | None = None
    apikey: str | None = None


@app.post("/api/test/sab")
def test_sab(req: TestSabRequest):
    url, apikey, why = _for_address(req.url, "sab_url", req.apikey, "sab_apikey")
    if why:
        return {"ok": False, "error": why}
    if not (url and apikey):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.sabnzbd_client import SABnzbdClient
        data = SABnzbdClient(url, apikey)._api(mode="queue")
        if data.get("status") is False:
            return {"ok": False, "error": data.get("error", "API rejected")}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": _redact(e, apikey)}


class TestQbitRequest(BaseModel):
    url: str | None = None
    user: str | None = None
    password: str | None = None


@app.post("/api/test/qbit")
def test_qbit(req: TestQbitRequest):
    url, password, why = _for_address(req.url, "qbit_url", req.password, "qbit_pass")
    if why:
        return {"ok": False, "error": why}
    user = req.user or _stored("qbit_user")
    if not (url and user and password):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.qbittorrent_client import QBittorrentClient
        ok, detail = QBittorrentClient(url, user, password).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": _redact(detail, password)}
    except Exception as e:
        return {"ok": False, "error": _redact(e, password)}


class TestProwlarrRequest(BaseModel):
    url: str | None = None
    apikey: str | None = None


@app.post("/api/test/prowlarr")
def test_prowlarr(req: TestProwlarrRequest):
    url, apikey, why = _for_address(req.url, "prowlarr_url", req.apikey, "prowlarr_apikey")
    if why:
        return {"ok": False, "error": why}
    if not (url and apikey):
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.prowlarr_client import ProwlarrClient
        ok, detail = ProwlarrClient(url, apikey).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": _redact(detail, apikey)}
    except Exception as e:
        return {"ok": False, "error": _redact(e, apikey)}


class TestFlareSolverrRequest(BaseModel):
    url: str | None = None


@app.post("/api/test/flaresolverr")
def test_flaresolverr(req: TestFlareSolverrRequest):
    url = req.url or _stored("flaresolverr_url")
    if not url:
        return {"ok": False, "error": "Not configured"}
    from kometa import flaresolverr
    ok, detail = flaresolverr.test(url)
    return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": detail}


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


@app.post("/api/test/hardcover")
def test_hardcover():
    """One light catalogue question with the stored token (never echoed)."""
    from kometa import hardcover
    ok, detail = hardcover.test(DB_PATH)
    return {"ok": True, "detail": detail} if ok else {"ok": False, "error": detail}


@app.post("/api/test/comicvine")
def test_comicvine(req: TestComicVineRequest):
    apikey = req.apikey or _stored("cv_api_key")
    if not apikey:
        return {"ok": False, "error": "Not configured"}
    try:
        from kometa.comicvine_client import ComicVineClient
        ok, detail = ComicVineClient(apikey).test()
        return {"ok": ok, "detail": detail} if ok else {"ok": False, "error": _redact(detail, apikey)}
    except Exception as e:
        return {"ok": False, "error": _redact(e, apikey)}


# Integration → config keys it owns. Drives 'Disconnect': blank password fields
# mean 'keep current', so without this there is NO path to remove a credential.
_INTEGRATION_KEYS = {
    "sabnzbd":   ["sab_url", "sab_apikey"],
    "qbit":      ["qbit_url", "qbit_user", "qbit_pass"],
    "prowlarr":  ["prowlarr_url", "prowlarr_apikey"],
    "flaresolverr": ["flaresolverr_url"],
    "comicvine": ["cv_api_key"],
    "gcd":       ["gcd_user", "gcd_pass"],
    "hardcover": ["hardcover_token"],
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


_EXTRA_INTEGRATION_KEYS = {"locg": ["locg_cf_clearance", "locg_user_agent", "locg_cf_set_at", "locg_impersonate"],
                           "fandom": ["fandom_cf_clearance", "fandom_user_agent", "fandom_cf_set_at"]}


def _backup_last() -> dict:
    try:
        from kometa import backup
        return backup.last(DB_PATH)
    except Exception:
        return {}


def _locg_status() -> dict:
    """Token live / paused until / what the top-up queue holds (kometa/topup.py)."""
    try:
        from kometa import topup
        return topup.status(DB_PATH)
    except Exception:
        return {"locg_pass_live": False, "locg_paused_until": None, "locg_topup_waiting": 0, "locg_topup_by_kind": {}}


def _extra_budgets() -> dict:
    """Today's requests per polite source — the Settings cards' only line."""
    out = {}
    try:
        from kometa import cbr, hardcover, openlibrary, prh
        for name, mod in (("prh", prh), ("openlibrary", openlibrary), ("cbr", cbr), ("hardcover", hardcover)):
            out[name] = mod.limiter.budget()
    except Exception:
        pass
    try:
        from kometa import fandom_client
        out["fandom"] = fandom_client.budget()      # the wikis' one throttle: galleries + collected editions
    except Exception:
        pass
    return out


# Each Settings card's name, and what to DO about a dot — the reason alone said
# what broke, on hover, which an iPad never does
_ATTN_TITLES = {
    "root": "Comics Library", "metron": "Metron", "comicvine": "ComicVine", "gcd": "Grand Comics Database",
    "hardcover": "Hardcover", "locg": "League of Comic Geeks", "fandom": "Fandom wikis",
    "prh": "Penguin Random House", "openlibrary": "Open Library", "cbr": "ComicBookRoundup",
    "marveldc": "Marvel.com / DC.com", "prowlarr": "Prowlarr", "sabnzbd": "Usenet (SABnzbd)",
    "qbit": "Torrents (qBittorrent)", "harvest": "Library harvest", "backup": "Backup",
}
# The quiet harvesters: no login, no card of their own. Their notes live inside the
# Harvesters section, and a non-fatal one (resting, or a hand-off already passed
# on) can be dismissed until it clears and comes back.
# background sources with no card on the page; their trouble groups into Harvesters
# (dismissable), never the top list. LOCG joined here when FlareSolverr took over its
# access — nothing to paste, nothing to set.
_HARVESTERS = ("prh", "openlibrary", "cbr", "fandom", "locg")
# Level 'broken': set up fine, failing anyway, and nothing in Settings can change it —
# a hand-off, not an instruction. Counted on the badge like a fix, so it isn't missed.
_TELL = "Nothing to change here — tell Claude: "


def _attn_fix(card: str, kind: str | None, dry_day: bool = False) -> tuple[str, str]:
    """(level, fix) for an unwell source (kometa/health.py). A refusal is 'wait':
    amber on its own card, never counted — Kometa backs off by itself. A source
    that has FAILED (not refused) for a whole day is 'broken': their page shape
    changed, or ours did, and only a code change fixes it."""
    if kind == "refused":
        return "wait", "Nothing to do: Kometa backs off and tries again on its own."
    if dry_day:
        return "broken", _TELL + "this source has been failing for a day; its page may have changed."
    return "wait", "Their side is down or timing out. Kometa keeps retrying."


def _broken_items() -> list[dict]:
    """What only a code change can fix: the nightly backup not landing, Marvel.com
    matching nothing. Worded as a hand-off ('tell Claude'), counted like a fix."""
    out = []
    try:
        from kometa import backup
        b = backup.last(DB_PATH)
        started = b.get("started_at") or ""
        age_h, when = None, started[:10]
        try:
            from kometa.scheduler import TZ
            t = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
            when = t.astimezone(TZ).strftime("%-d %b %-I:%M%p").lower()      # the run's own clock, not UTC's date
        except ValueError:
            pass
        if b and not b.get("ok"):
            out.append({"card": "backup", "reason": f"last backup ({when}) failed: {str(b.get('error') or '')[:80]}", "fix": _TELL + "the nightly backup is failing.", "level": "broken"})
        elif age_h is not None and age_h > 30:
            # one run a night at 03:30: past 30 h, last night's didn't happen (the record is the one before)
            out.append({"card": "backup", "reason": f"last night's backup didn't run — the last good one was {when}",
                        "fix": _TELL + "the nightly backup missed a night.", "level": "broken"})
    except Exception:
        pass
    return out


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
        out.append({"card": "root", "reason": "comics folder missing or read-only",
                    "fix": "Check the NAS is up and its comics share is mounted on ginbako, then reload."})
    out += _broken_items()
    if on("metron_enabled"):
        from kometa import metron_client
        creds = metron_client._creds()
        if not creds:
            out.append({"card": "metron", "reason": "no login", "fix": "Add your Metron username and password in the Metron card."})
        elif metron_client._refused(creds):
            out.append({"card": "metron", "reason": "Metron refused the login",
                        "fix": "Re-enter your Metron password in the Metron card; it may have changed on metron.cloud."})
    if on("comicvine_enabled") and not has("cv_api_key"):
        out.append({"card": "comicvine", "reason": "no API key",
                    "fix": "Paste a key from comicvine.gamespot.com/api into the ComicVine card, or switch ComicVine off."})
    if on("gcd_enabled") and not has("gcd_user", "gcd_pass"):
        out.append({"card": "gcd", "reason": "no login",
                    "fix": "Add your comics.org login in the Grand Comics Database card, or switch it off."})
    if on("hardcover_enabled") and not has("hardcover_token"):
        out.append({"card": "hardcover", "reason": "no API token",
                    "fix": "Paste a token from hardcover.app › Settings › API into the Hardcover card, or switch it off."})
    # PRH, Open Library and ComicBookRoundup need no login — nothing for you to fix,
    # so never a dot (a refusal just rests that source; the harvest card shows it)
    # LOCG and Fandom wait on a pass only a person can give; the page no longer nags
    # about it (2026-10-15) — their cards show the field and the queue line, nothing more.
    # set up fine, failing anyway (kometa/health.py) — a switched-off source never nags
    try:
        from kometa import health
        keys = {"metron": "metron_enabled", "locg": "locg_enabled", "gcd": "gcd_enabled", "comicvine": "comicvine_enabled",
                "fandom": "fandom_enabled", "hardcover": "hardcover_enabled", "prh": "prh_enabled",
                "openlibrary": "openlibrary_enabled", "cbr": "cbr_enabled"}
        lit = {i["card"] for i in out}
        for u in health.unwell(DB_PATH):
            if on(keys.get(u["card"], "_")) and u["card"] not in lit:
                level, fix = _attn_fix(u["card"], u.get("kind"), u.get("dry_day", False))
                out.append({"card": u["card"], "reason": u["reason"], "fix": fix, "level": level})
                lit.add(u["card"])
    except Exception:
        pass
    if on("prowlarr_enabled"):
        if not has("prowlarr_url", "prowlarr_apikey"):
            out.append({"card": "prowlarr", "reason": "not set up", "fix": "Fill in the Prowlarr URL and API key, or switch Prowlarr off."})
        if on("usenet_enabled") and not has("sab_url", "sab_apikey"):
            out.append({"card": "sabnzbd", "reason": "not set up", "fix": "Fill in the SABnzbd URL and API key, or switch Usenet off."})
        if on("torrent_enabled") and not has("qbit_url", "qbit_user"):
            out.append({"card": "qbit", "reason": "not set up", "fix": "Fill in the qBittorrent URL and login, or switch Torrents off."})
    for i in out:
        i["title"] = _ATTN_TITLES.get(i["card"], i["card"])
        i.setdefault("level", "fix")
        if i["card"] in _HARVESTERS:
            i["group"] = "harvesters"
    return _apply_dismissals(out)


def _dismissed() -> dict:
    try:
        return json.loads(db.get_config(DB_PATH).get("attention_dismissed") or "{}")
    except Exception:
        return {}


def _apply_dismissals(items: list[dict]) -> list[dict]:
    """Drop a dismissed (card, level); forget a dismissal once its item has gone, so
    the same trouble coming back later shows again. 'fix' is never dismissable."""
    d = _dismissed()
    for i in items:
        i["dismissable"] = i["level"] != "fix"
    if not d:
        return items
    live = {i["card"]: i["level"] for i in items}
    kept = [i for i in items if not (i["level"] != "fix" and d.get(i["card"]) == i["level"])]
    stale = [c for c in d if c not in live]
    if stale:
        for c in stale:
            d.pop(c, None)
        db.set_config({"attention_dismissed": json.dumps(d)}, DB_PATH)
    return kept


class DismissRequest(BaseModel):
    card: str
    level: str


@app.post("/api/settings/attention/dismiss")
def dismiss_attention(req: DismissRequest):
    if req.level == "fix":
        raise HTTPException(400, "That one is yours to fix")
    d = _dismissed()
    d[req.card] = req.level
    db.set_config({"attention_dismissed": json.dumps(d)}, DB_PATH)
    return {"ok": True}


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
    """'Top up now': drain the LOCG queue in a thread while LOCG is reachable."""
    from kometa import topup, locg_client
    if locg_client.locg_paused() is not None:
        return {"ok": False, "started": False, "error": "LOCG is paused", **topup.status(DB_PATH)}
    started = topup.drain_in_background()
    return {"ok": True, "started": started, **topup.status(DB_PATH)}


@app.post("/api/test/locg")
def test_locg_access():
    """Settings 'test' for the LOCG card. With a token set: try every client
    profile, remember the one LOCG accepts (config locg_impersonate), lift the
    pause. None accepted → the token is dropped. No token: a single probe."""
    from kometa import locg_client as lc
    if lc._access()[0]:
        results = lc.probe_matrix()
        hit = next((r for r in results if r["status"] == 200), None)
        if hit:
            db.set_config({"locg_impersonate": hit["target"], "locg_paused_until": "0"}, DB_PATH)
            lc._pause["until"] = 0.0
            lc._access_cache["ts"] = 0.0
            # drain what was waiting, in the background
            from kometa import topup
            topup.drain_in_background()
            return {"ok": True, "detail": f"LOCG accepted the {hit['target']} profile — pause lifted"}
        lc._forget_access()
        tried = ", ".join(f"{r['target']} {r['status'] or 'err'}" for r in results)
        return {"ok": False, "error": f"LOCG refused every profile ({tried})"}
    r = lc.probe()
    return {"ok": True, "detail": r["detail"]} if r["ok"] else {"ok": False, "error": r["detail"]}


@app.post("/api/test/fandom")
def test_fandom_access():
    """Settings 'test' for the Fandom card: one gallery image through the lane.
    200 = the token works (the lane's rest lifts); 403 = it's dropped."""
    from kometa import images, polite
    cf, ua = images._fandom_pass(DB_PATH)
    if not (cf and ua):
        return {"ok": False, "error": "no pass set"}
    url = None
    with db._connect(DB_PATH) as conn:
        for (raw,) in conn.execute("""SELECT extra_json FROM issue_record WHERE extra_json LIKE '%"fandom": {%"covers": [{%' LIMIT 1"""):
            import json as _json
            covers = ((_json.loads(raw or "{}").get("fandom") or {}).get("covers") or [])
            url = covers[0].get("url") if covers else None
    if not url:
        return {"ok": False, "error": "no gallery listed yet to test with"}
    images.fandom_pass_renewed(DB_PATH, start=False)     # the test's own request first; a working token starts the burst
    try:
        data = images._lane_fetch(images._lane_limiter(images.FANDOM_HOST), images.FANDOM_HOST, DB_PATH)(url)
    except polite.SourceUnavailable as e:
        return {"ok": False, "error": _scrub.scrub(f"Fandom refused us — token dropped ({e})" if "(403)" in str(e) else str(e))}
    if not data:
        return {"ok": False, "error": "Fandom answered, but not with an image"}
    images.fandom_pass_renewed(DB_PATH)
    return {"ok": True, "detail": f"Fandom accepted the token ({len(data) // 1024} KB image) — downloading now"}


# --- version ---

@app.get("/api/version")
def get_version(request: Request):
    """What commit is this, and is the running process actually it?

    The honest answer to "did my deploy land", which nothing else here could
    give you. From the internet with no token it says only that a Kometa lives
    here (YondeB's probe wants a `sha` key, nothing more): the exact commit of a
    public repo is a map for anyone hunting a known bug.
    """
    from kometa.auth import via_tunnel, ANONYMOUS, current_user
    if via_tunnel(request.scope) and current_user() is ANONYMOUS:
        return {"app": "kometa", "sha": ""}
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
        "flaresolverr_url":    cfg.get("flaresolverr_url", ""),
        "flaresolverr_configured": bool(cfg.get("flaresolverr_url", "")),
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
        # the polite extras (harvest 1i): no login, just on/off and today's allowance
        "prh_enabled":         cfg.get("prh_enabled", "1") != "0",
        "openlibrary_enabled": cfg.get("openlibrary_enabled", "1") != "0",
        "cbr_enabled":         cfg.get("cbr_enabled", "1") != "0",
        # Fandom galleries (1i-d): listing on by default; downloading OFF until asked
        "fandom_enabled":      cfg.get("fandom_enabled", "1") != "0",
        "fandom_download":     cfg.get("fandom_download", "0") == "1",
        # Hardcover (1i-f): book ratings by ISBN with your token — never echoed
        "hardcover_token":     "",
        "hardcover_configured": bool((cfg.get("hardcover_token") or "").strip()),
        "hardcover_enabled":   cfg.get("hardcover_enabled", "1") != "0",
        # Marvel.com print covers (1i-e): night only, on by default (the user's call, 2026-10-12)
        "extra_budgets":       _extra_budgets(),
        # Metron: the primary metadata source (docs/reader-spec.md, 2026-10-08)
        "metron_user":         cfg.get("metron_user", ""),
        "metron_pass":         "",
        "metron_token":        "",
        "metron_token_set":    bool((cfg.get("metron_token") or "").strip()),
        "metron_configured":   bool((cfg.get("metron_token") or "").strip() or (cfg.get("metron_user", "") and cfg.get("metron_pass", ""))),
        "metron_enabled":      cfg.get("metron_enabled", "1") != "0",
        # LOCG token (kometa/locg_client.py). Never echoed back.
        "locg_cf_configured":  bool(cfg.get("locg_cf_clearance", "")),
        "locg_enabled":        cfg.get("locg_enabled", "1") != "0",
        "locg_cf_set_at":      cfg.get("locg_cf_set_at", ""),
        "locg_user_agent":     cfg.get("locg_user_agent", ""),
        # Fandom's image CDN token (kometa/images.py). Never echoed back.
        "fandom_cf_configured": bool(cfg.get("fandom_cf_clearance", "")),
        "fandom_cf_set_at":    cfg.get("fandom_cf_set_at", ""),
        "fandom_user_agent":   cfg.get("fandom_user_agent", ""),
        # step 5: record → our own ComicInfo → online for the issue sheet. OFF until
        # the full ComicInfo rewrite has run (kometa/localfirst.py); no UI yet
        "read_local_first":    cfg.get("read_local_first", "0") == "1",
        # the credits ladder (record.fill_credits): GCD, then Comic Vine, for issues Metron
        # left without credits. Off until the owner's OK — given 2026-10-17
        "credits_ladder":      cfg.get("credits_ladder") == "on",
        # its Comic Vine rung by the run's name, for issues no catalogue gave an id (kometa/cvname.py)
        "cv_by_name":          cfg.get("cv_by_name") == "on",
        # on-sale dates, cover dates and descriptions for old issues (kometa/gapfill.py)
        "gap_fill":            cfg.get("gap_fill") == "on",
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
    flaresolverr_url:   str | None = None   # FlareSolverr service URL (NAS :8191)
    cv_api_key:         str | None = None
    prowlarr_enabled:   str | None = None   # "1"/"0" — master search toggle
    usenet_enabled:     str | None = None   # "1"/"0" — search-source toggle
    torrent_enabled:    str | None = None
    comicvine_enabled:  str | None = None   # "1"/"0" — metadata source toggle
    gcd_user:           str | None = None
    gcd_pass:           str | None = None
    gcd_enabled:        str | None = None   # "1"/"0" — metadata source toggle
    prh_enabled:        str | None = None   # "1"/"0" — PRH print covers (no login)
    openlibrary_enabled: str | None = None  # "1"/"0" — Open Library ratings + covers (no login)
    cbr_enabled:        str | None = None   # "1"/"0" — ComicBookRoundup scores (no login)
    fandom_enabled:     str | None = None   # "1"/"0" — Fandom cover galleries listed into the record
    fandom_download:    str | None = None   # "1"/"0" — and their images downloaded (default off)
    hardcover_token:    str | None = None   # Hardcover API token (read:catalog)
    hardcover_enabled:  str | None = None   # "1"/"0" — Hardcover book ratings
    cbr_people_enabled: str | None = None   # "1"/"0" — ComicBookRoundup's critics + readers, overnight
    cbr_people_hours:   str | None = None   # "23-7": the local hours it may run in (wraps midnight)
    push_enabled:       str | None = None   # "1"/"0" — notify YondeB when a download lands
    push_scope:         str | None = None   # "pull" (pull-list series only) | "all"
    apns_key_id:        str | None = None   # the .p8's key id; the key itself sits in data/apns/
    apns_team_id:       str | None = None
    metron_user:        str | None = None
    metron_pass:        str | None = None
    metron_token:       str | None = None   # API token (preferred over user/pass)
    metron_enabled:     str | None = None   # "1"/"0" — primary metadata source
    locg_enabled:       str | None = None   # "1"/"0" — off = not one request to LOCG
    locg_cf_clearance:  str | None = None   # LOCG access token (expires)
    locg_user_agent:    str | None = None   # the UA the token was issued to
    fandom_cf_clearance: str | None = None  # Fandom image CDN access token
    fandom_user_agent:  str | None = None   # the UA the token was issued to
    read_local_first:   str | None = None   # "1"/"0" — issue details from the record + our ComicInfo first (default off)
    retag_batch_armed:  str | None = None   # "1"/"0" — the full ComicInfo rewrite's routes answer (kometa/retag_batch.py)
    credits_ladder:     str | None = None   # "on"/"off" — the credits ladder (record.credits_ladder_on)
    cv_by_name:         str | None = None   # "on"/"off" — its Comic Vine rung by the run's name (cvname.configured)
    gap_fill:           str | None = None   # "on"/"off" — dates and descriptions for old issues (gapfill.on)


@app.patch("/api/config")
def update_config(req: ConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None and v != ""}
    for k in ("credits_ladder", "cv_by_name", "gap_fill"):
        if k in updates and updates[k] not in ("on", "off"):
            raise HTTPException(400, f"{k} is on or off")
    if "comics_root" in updates:
        try:
            check_comics_root(updates["comics_root"])
        except OutsideLibrary as e:
            raise HTTPException(422, str(e))
    if "cbr_people_hours" in updates:
        from kometa.marveldc import valid_hours
        if not valid_hours(updates["cbr_people_hours"]):
            raise HTTPException(400, "hours are two numbers 0-23, e.g. 23-7")
        updates["cbr_people_hours"] = updates["cbr_people_hours"].replace(" ", "")
    if "locg_cf_clearance" in updates:
        updates["locg_cf_clearance"] = updates["locg_cf_clearance"].strip().removeprefix("cf_clearance=")
        updates["locg_cf_set_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if "fandom_cf_clearance" in updates:
        updates["fandom_cf_clearance"] = updates["fandom_cf_clearance"].strip().removeprefix("cf_clearance=")
        updates["fandom_cf_set_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.set_config(updates, DB_PATH)
    if "fandom_cf_clearance" in updates:
        from kometa.images import fandom_pass_renewed
        fandom_pass_renewed(DB_PATH)                       # the lane starts now, not at the next tick
    if "locg_cf_clearance" in updates:
        # the Settings field re-verifies on save (POST /api/test/locg) — one request,
        # and on success the pause lifts right there
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


_SERIES_LIST_UNUSED = ("metron_fetched_at", "added_at", "metron_checked_at",
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


# The family map (which special belongs to which run) is a whole-library
# computation: every series' counts aggregated over 27k issue rows, then the title
# matching. It was rebuilt for EVERY series page — 17.7 of the page's 17.8 ms.
# Now it's kept for five minutes, or until the series table changes shape (a
# series added, removed or re-synced), whichever comes first.
_family_cache = {"ts": 0.0, "fp": None, "rows": {}}
_FAMILY_TTL = 300


def _family_fingerprint() -> tuple:
    with db._connect(DB_PATH) as conn:
        return tuple(conn.execute("SELECT COUNT(*), MAX(id), MAX(last_synced) FROM tracked_series").fetchone())


def _family_rows() -> dict:
    from kometa.family import attach_families
    fp = _family_fingerprint()
    now = time.time()
    if _family_cache["fp"] == fp and now - _family_cache["ts"] < _FAMILY_TTL:
        return _family_cache["rows"]
    summaries = db.get_all_series_summaries(DB_PATH)
    rows = [dict(s, **summaries.get(s["id"], {})) for s in db.get_all_series(DB_PATH) if s.get("kind") != "arc"]
    rows = {r["id"]: r for r in attach_families(rows)}
    _family_cache.update(ts=now, fp=fp, rows=rows)
    return rows


def forget_families() -> None:
    _family_cache["ts"] = 0.0


def _family_for(series_id: int) -> dict:
    """The series' family for its page: the parent (if it is a special) and its
    specials (if it is a run), as light rows the cards can draw."""
    try:
        rows = _family_rows()
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
        # is the only ownership source — a catalogue entry named after the arc does
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
    return dict(s, issues=issues, trade_count=trade_count, has_trades=has_trades, **_series_marks(series_id), **_family_for(series_id),
                arc_count=arc_count, shelf_id=db.shelf_id_for_series(series_id, DB_PATH),
                run_status=runstatus.compute(s, issues), **_summary(issues))






























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
    from kometa.trade_community import for_trade
    local = trade_details(locg_id)
    if local is None:
        try:
            local = _locg_issue_details(locg_id)
        except Exception as e:
            raise HTTPException(502, f"LOCG details failed: {e}")
    # the book sites' ratings for this edition, by its ISBN (Hardcover, Open Library)
    from kometa.auth import is_admin
    from kometa.related import with_creator_ids
    return {**local, "credits": with_creator_ids(local.get("credits") or [], DB_PATH, ask=is_admin()),
            "community": for_trade(locg_id)}


@app.get("/api/books/{book_id}/community")
def get_book_community(book_id: int):
    """The shelf's book sheet: a trade's community ratings, found through its run's
    trades list ({"community": null} for anything that isn't a rated trade), and
    an edition of a one-book run's own details — description, credits, release
    date — borrowed from that book (kometa/edition_details.py; null otherwise)."""
    from kometa.trade_community import for_book
    from kometa import edition_details
    from kometa.related import with_creator_ids_asking
    details = edition_details.for_book(book_id, DB_PATH)
    if details:
        details["credits"], _ = with_creator_ids_asking(details["credits"], DB_PATH, ask=False)
    return {"community": for_book(book_id), "details": details}


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
    try:
        check_series_folder(folder_path, _comics_root())
    except OutsideLibrary as e:
        raise HTTPException(422, str(e))

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
        if req.on_pull_list and _auto_pull(new_id, db.get_issues_for_series(new_id, DB_PATH)):
            _process_queue()

    threading.Thread(target=_bg_sync, daemon=True).start()
    return added


PULL_AUTO_CAP = 33          # the owner's number ("33 just cause", 2026-10-16)


def _auto_pull(series_id: int, issues: list[dict], today: str | None = None, path=None) -> int:
    """The pull rule for a run someone asked for (Track from a card, Want to read on a run):
    PULL_AUTO_CAP or fewer released, missing issues → the pull list goes ON and all of them
    are queued. More → the pull list goes OFF and only the FIRST PULL_AUTO_CAP are queued, from
    #1 up (the owner, 2026-10-24 — it used to queue nothing at all). One tap still never
    starts a 500-file grab of Batman (1940); the rest wait for the series page's toggle or ↓.
    Returns how many were queued."""
    path = path or DB_PATH
    today = today or str(date.today())
    # Batch the queue insert — per-issue queue_issue is one fresh connection + fsync EACH,
    # which the NAS disk does not appreciate.
    pairs = [(series_id, issue["number"]) for issue in sorted(issues, key=lambda i: i["number"])
             if not issue["owned"] and not issue.get("ignored")
             and (not issue["store_date"] or issue["store_date"] <= today)]
    over = len(pairs) > PULL_AUTO_CAP
    db.set_pull_list(series_id, not over, path)
    if over:
        logger.info(f"Track: {len(pairs)} missing issues is over the auto-pull cap of {PULL_AUTO_CAP} — "
                    f"pull list off for series {series_id}, the first {PULL_AUTO_CAP} queued")
        pairs = pairs[:PULL_AUTO_CAP]
    if pairs:
        db.queue_issues_bulk(pairs, path)
    return len(pairs)


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
    metron_extra_ids: list[int] | None = None   # more Metron runs in the same numbering ([] clears)
    gcd_id: int | None = None   # the Grand Comics Database run (harvest 1g); 0 = "not on GCD"
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
        db.set_gcd_link(series_id, "none", path=DB_PATH)
        # a person said there is no run: neither the re-match job nor the
        # re-check queue ever asks about it again
        from kometa.rematch import NEVER, ensure_columns
        ensure_columns(DB_PATH)
        with db._connect(DB_PATH) as conn:
            conn.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (NEVER, series_id))
        return db.get_series_by_id(series_id, DB_PATH)
    if req.gcd_id is not None:
        # GCD's link stands apart: no sync, no match status — the harvest's fill
        # tick picks the run up (an answer to GET /api/harvest/gcd-candidates)
        if req.gcd_id > 0:
            db.set_gcd_link(series_id, "linked", req.gcd_id, path=DB_PATH)
        else:
            db.set_gcd_link(series_id, "none", path=DB_PATH)
        if not (req.locg_id or req.metron_id or req.metron_extra_ids is not None):
            return db.get_series_by_id(series_id, DB_PATH)
    if not (req.locg_id or req.metron_id or req.metron_extra_ids is not None):
        raise HTTPException(400, "Pick a run")
    if req.locg_id:
        db.set_locg_series_id(series_id, req.locg_id, DB_PATH)
    if req.metron_id:
        db.set_metron_series_id(series_id, req.metron_id, DB_PATH)
        db.set_metron_link(series_id, "linked", DB_PATH)
    if req.metron_extra_ids is not None:
        db.set_metron_extra_ids(series_id, req.metron_extra_ids, DB_PATH)
    db.set_match_status(series_id, "manual", DB_PATH)
    threading.Thread(target=sync_one_guarded,
                     args=(db.get_series_by_id(series_id, DB_PATH), lambda x: _sync_one(x, force=True)),
                     daemon=True).start()
    return db.get_series_by_id(series_id, DB_PATH)


class UnpinnedApply(BaseModel):
    series_ids: list[int] | None = None     # from the proposals in memory
    links: list[dict] | None = None         # or the approved links themselves: [{series_id, cv?, gcd?}]


@app.post("/api/unpinned")
def unpinned_start():
    """Ask Comic Vine and GCD again for the runs no catalogue pinned down; proposals only
    (kometa/unpinned.py). Minutes: GET /api/unpinned for progress and the proposals."""
    from kometa import unpinned
    return unpinned.start()


@app.get("/api/unpinned")
def unpinned_state():
    from kometa import unpinned
    return unpinned.state()


@app.post("/api/unpinned/apply")
def unpinned_apply(req: UnpinnedApply):
    """Link the proposals the owner approved."""
    from kometa import unpinned
    return unpinned.apply(req.series_ids, links=req.links)


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


class SourcingConfirmRequest(BaseModel):
    series_id: int
    metron_series_id: int


class SourcingDeclineRequest(BaseModel):
    series_id: int


@app.get("/api/sourcing/candidates")
def sourcing_candidates():
    from kometa import sourcing
    sourcing.ensure_deck_rescored(DB_PATH)
    with db._connect(DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, s.title, s.publisher, s.year_began, s.folder_path, s.locg_series_id, s.metron_candidates_json,
                   (SELECT COUNT(*) FROM books WHERE tracked_series_id = s.id) AS book_count,
                   (SELECT COUNT(*) FROM issue_status WHERE tracked_series_id = s.id AND owned = 1) AS owned_count,
                   (SELECT COUNT(*) FROM issue_status WHERE tracked_series_id = s.id AND owned = 0) AS missing_count
            FROM tracked_series s
            WHERE s.locg_series_id IS NOT NULL
              AND s.metron_series_id IS NULL
              AND s.metron_candidates_json IS NOT NULL
              AND s.metron_candidates_json != '[]'
              AND (s.locg_link IS NULL OR s.locg_link != 'none')
              AND (s.metron_checked_at IS NULL OR s.metron_checked_at != '9999-12-31 00:00:00')
            ORDER BY s.title COLLATE NOCASE""")]
    out = []
    for r in rows:
        cands = json.loads(r.get("metron_candidates_json") or "[]")
        if cands:
            out.append({
                "id": r["id"],
                "title": r["title"],
                "publisher": r["publisher"],
                "year": r["year_began"],
                "folder_path": r["folder_path"],
                "locg_series_id": r["locg_series_id"],
                "total_issues": max(r["book_count"], r["owned_count"] + r["missing_count"]),
                "candidates": cands,
            })
    return {"items": out}


@app.post("/api/sourcing/confirm")
def sourcing_confirm(req: SourcingConfirmRequest):
    s = db.get_series_by_id(req.series_id, DB_PATH)
    if not s:
        raise HTTPException(404, "Series not found")
    with db._connect(DB_PATH) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = ? WHERE id = ?", (req.metron_series_id, req.series_id))
    return {"ok": True, "series_id": req.series_id, "metron_series_id": req.metron_series_id}


@app.post("/api/sourcing/decline")
def sourcing_decline(req: SourcingDeclineRequest):
    s = db.get_series_by_id(req.series_id, DB_PATH)
    if not s:
        raise HTTPException(404, "Series not found")
    db.set_metron_link(req.series_id, "none", DB_PATH)
    with db._connect(DB_PATH) as conn:
        conn.execute("UPDATE tracked_series SET metron_candidates_json = '[]' WHERE id = ?", (req.series_id,))
    return {"ok": True, "series_id": req.series_id}


@app.get("/api/sourcing/preview/{metron_id}")
def sourcing_preview(metron_id: int):
    from kometa import metron_client
    try:
        d = metron_client.series_detail(metron_id)
        if not d:
            raise HTTPException(404, "Metron series not found")
        pub = d.get("publisher")
        pub_name = pub.get("name") if isinstance(pub, dict) else (pub or "")
        return {
            "id": metron_id,
            "title": d.get("name") or d.get("title") or "",
            "publisher": pub_name,
            "year": d.get("year"),
            "issue_count": d.get("issue_count") or d.get("count"),
        }
    except metron_client.MetronUnavailable as e:
        raise HTTPException(503, str(e))


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

class WheelRecording(BaseModel):
    note: str | None = None
    ua: str | None = None
    swipes_intended: int | None = None
    events: list[list[float]]          # [timestamp_ms, deltaX, deltaY], as the browser delivered them


@app.post("/api/debug/wheel-recording")
def save_wheel_recording(rec: WheelRecording):
    """The swipe recorder (/swipe-recorder.html): a real trackpad's wheel stream, kept
    so the reader's one-page-per-swipe rule (_rdWheelGesture) is tuned to — and
    tested against — what the hardware actually sends."""
    import json as _json
    folder = os.path.join(os.path.dirname(DB_PATH) or ".", "wheel-recordings")
    os.makedirs(folder, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json"
    with open(os.path.join(folder, name), "w") as fh:
        _json.dump(rec.model_dump(), fh)
    return {"ok": True, "file": name, "events": len(rec.events)}


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


def _within(path: str, root: str) -> bool:
    """path is root or under it — not '/comics2' passing for '/comics'."""
    return root == "/" or path == root or path.startswith(root.rstrip(os.sep) + os.sep)


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
    if not _within(target, root):
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
        "parent": parent if _within(parent, root) and parent != target else None,
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
    if req.scope == "fs" and os.path.isdir(_comics_root()):
        raise HTTPException(403, "New folders go inside the comics folder")   # anywhere else: first-run setup only
    root = "/" if req.scope == "fs" else os.path.realpath(_comics_root())
    parent = os.path.realpath(req.path or root)
    if not _within(parent, root):
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
    if req.folder_path:
        try:
            check_series_folder(req.folder_path, _comics_root())
        except OutsideLibrary as e:
            raise HTTPException(422, str(e))
    db.set_folder_path(series_id, req.folder_path or None, DB_PATH)
    s = db.get_series_by_id(series_id, DB_PATH)
    _rescan_owned(s)   # scan the just-set folder → mark which issues are owned
    return db.get_series_by_id(series_id, DB_PATH)



# --- download queue ---

# what no screen reads: indexer URLs with API keys in them (56 KB of the 171 KB
# queue) and the per-row blacklists the acquisition side keeps for itself
_QUEUE_PRIVATE = ("source_url", "failed_sources", "failed_channels", "sab_nzo_id", "torrent_hash")


@app.get("/api/changes")
def changes():
    """A stamp that moves when what the shelf pages show moves: a search queued or a
    download landing (the queue), a file arriving (books), an issue becoming owned, a
    place kept on another device. The pages poll it and repaint only when it moves —
    Buffy's page sat still after its pull list went on (the owner, 2026-10-18)."""
    # One stamp per thing a page can show, so a page only repaints for what it shows (2026-10-29):
    # a page turn on the iPad moves `progress` (On Deck's Continue reading) but not `done` (the
    # series page's and the Library's read marks) — before, one stamp moved with every turn and
    # every open web view repainted itself, backdrop and all, every 10 s while anyone read.
    with db._connect(DB_PATH) as conn:
        q = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM download_queue").fetchone()
        b = conn.execute("SELECT COUNT(*), MAX(id) FROM books").fetchone()
        o = conn.execute("SELECT SUM(owned) FROM issue_status").fetchone()
        p = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM read_progress").fetchone()
        d = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM read_progress WHERE completed = 1").fetchone()
    j = lambda *xs: "|".join(str(x) for x in xs)
    return {"stamp": j(*q, *b, *o, *p), "queue": j(*q), "books": j(*b), "owned": j(*o), "progress": j(*p), "done": j(*d)}


@app.get("/api/queue/summary")
def queue_summary(since: str = ""):
    """The activity badge's three numbers — failed, not found, done since you
    last looked — without the whole queue (it polled all of it every 25 s)."""
    db.close_rows_now_owned(path=DB_PATH)
    with db._connect(DB_PATH) as conn:
        rows = conn.execute("""SELECT q.state, COUNT(*) AS n FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state IN ('failed', 'not_found', 'done') AND q.updated_at > ? GROUP BY q.state""", (since or "",)).fetchall()
    got = {r["state"]: r["n"] for r in rows}
    return {"failed": got.get("failed", 0), "not_found": got.get("not_found", 0), "done": got.get("done", 0)}


# Rows whose screen changes with no write to their row: download progress and the
# search line live in memory, and an issue waiting on a trade shows the trade's fate.
# A ?since= answer always carries these.
_QUEUE_LIVE = frozenset(("queued", "searching", "found", "downloading", "pending_usenet", "pending_torrent",
                         "processing", "proposed", "suggested", "via_trade"))


@app.get("/api/queue")
def get_queue(since: str | None = None):
    """The whole queue, newest first. With ?since= (Activity polls every 2 s while
    something's moving): {at, rows, states} — `rows` is what changed at or after
    `since` plus every live row, `states` is [id, state] for every row so the page can
    drop deleted ones and catch a state that changed without its time moving (a trade
    covering a stuck single). Pass `at` back as the next `since`; since= alone is
    the whole queue in that shape."""
    db.close_rows_now_owned(path=DB_PATH)
    at = None
    if since is not None:
        with db._connect(DB_PATH) as conn:
            at = conn.execute("SELECT datetime('now')").fetchone()[0]     # before the read: nothing slips between
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
        item["next_try"] = next_try(item)
        for k in _QUEUE_PRIVATE:
            item.pop(k, None)
        prog = get_progress(item["id"])
        if prog:
            item["progress"] = prog
        if item["state"] in ("searching", "pending_usenet"):     # pending_usenet: 'waiting in SAB' line
            ss = get_search_status(item["id"])
            if ss:
                item["search_status"] = ss
    if since is None:
        return items
    return {"at": at, "states": [[i["id"], i["state"]] for i in items],
            "rows": [i for i in items if i["state"] in _QUEUE_LIVE or (i.get("updated_at") or "") >= since]}


@app.delete("/api/queue/{queue_id}", status_code=204)
def delete_queue_item(queue_id: int):
    # a running download stops with its row, instead of carrying on unseen
    from kometa.acquisition import stop_download
    try:
        stop_download(queue_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.remove_queue_item(queue_id, DB_PATH)


@app.post("/api/queue/retry-not-found")
def retry_not_found():
    """Bulk re-search of everything that came up empty — pull-to-refresh on
    Activity. failed items are excluded; they have per-row Retry."""
    db.close_rows_now_owned(path=DB_PATH)
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
        db.forget_failed_releases(queue_id, DB_PATH)
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


class DescRequest(BaseModel):
    desc: str


@app.patch("/api/series/{series_id}/issues/{number}/desc")
def set_issue_desc(series_id: int, number: float, req: DescRequest):
    """Put right a catalogue's mangled description (kometa/record.py set_issue_desc); it
    reaches the file's ComicInfo with the next retag. Empty hands it back to the catalogues."""
    from kometa import record, retag
    r = record.set_issue_desc(series_id, number, req.desc, DB_PATH)
    if not r:
        raise HTTPException(404, "No record for that issue")
    retag.requeue_issue_now(series_id, number, DB_PATH)
    return {"ok": True, "desc": r.get("desc") or ""}


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
    if not images.ext_cached(u, DB_PATH):
        from kometa.auth import current_user
        if not images.ext_budget_ok(current_user()["id"]):
            raise HTTPException(429, "Too many new images this hour")
    resp = images.serve_external(u, DB_PATH)
    if resp is None:
        raise HTTPException(404)
    return resp


@app.get("/api/series/{series_id}/issues/{number}/locg-details")
def get_issue_locg_details(series_id: int, number: float):
    """The issue card's whole answer, the same on the web and both apps (2026-10-17):
    its header — the run, publisher and year, the date, owned or ignored, the run's
    status, the book behind an owned issue — then the details: description and credits,
    Metron first, LOCG for issues Metron lacks (kometa.issue_meta), and what readers and
    critics say. An issue no catalogue knows still gets its header. Route name kept for
    the frontends; cached either way (kometa-recommend also reads the LOCG entries)."""
    from kometa import record
    from kometa.auth import is_admin
    s = db.get_series_by_id(series_id, DB_PATH)
    issues = db.get_issues_for_series(series_id, DB_PATH) if s else []
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue:
        raise HTTPException(404)
    d = {}
    from kometa import localfirst
    # no catalogue id: only read-local-first can answer (from the record or our own tags)
    if issue.get("locg_issue_id") or issue.get("metron_issue_id") or localfirst.enabled(DB_PATH):
        # A reader never triggers a catalogue fetch — and nobody waits on LOCG's issue pages, which
        # are walled: Absolute Batman #26 (no Metron id yet, nothing stored) took 5 s and a 502, and
        # the 502 took the card's header with it (2026-10-18). The record trickle keeps asking.
        live = is_admin() and bool(issue.get("metron_issue_id"))
        try:
            d = record.details(issue, DB_PATH, stored_only=not live)
        except Exception as e:
            raise HTTPException(502, "Details fetch failed") from e
    # what readers and critics say (LOCG, ComicBookRoundup, Hardcover, Open Library) —
    # the stored snapshot, never a fetch on open
    row = record.get_issue(series_id, number, DB_PATH) or {}
    from kometa.related import with_creator_ids_asking
    credits, asking = with_creator_ids_asking(d.get("credits", []), DB_PATH, ask=is_admin())
    with db._connect(DB_PATH) as conn:                   # the shelf index's book, if any: no folder listing here
        b = conn.execute("SELECT id FROM books WHERE tracked_series_id = ? AND number = ? ORDER BY mtime DESC LIMIT 1",
                         (series_id, number)).fetchone()
    return {"desc": d.get("desc", ""), "credits": credits,
            "ids_pending": asking,                       # names just sent to Metron: look again shortly
            "source": d.get("source"), "record": d.get("record"),
            # the on-sale date from the issue itself: a record's own date can be a file tag's bare
            # year saved as 1 January ('Released January 1, 2019'). No on-sale date → the cover
            # date, which the card shows as such (kometa/gapfill.py)
            "store_date": issue.get("store_date") or (
                d.get("store_date") if d.get("source") in ("metron", "locg") else None),
            "cover_date": issue.get("cover_date"),
            "community": row.get("community") or None,
            # the header, so every card shows the same facts without piecing them together
            "series_title": s["title"], "publisher": s.get("publisher"), "year_began": s.get("year_began"),
            "owned": bool(issue.get("owned")), "ignored": bool(issue.get("ignored")),
            "run_status": runstatus.compute(s, issues),
            "book_id": b["id"] if b and issue.get("owned") else None}


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
        from kometa.auth import is_admin
        data = record.variants(issue, DB_PATH, stored_only=not is_admin())   # a reader: the record or nothing
        covers = [dict(c) for c in data.get("covers", [])]
        from kometa import coverupgrade, images
        for c in covers:                                 # the large view: a stored bigger copy of the SAME image
            k = images.variant_key(series_id, number, c.get("id"))
            if c.get("id") and coverupgrade.better_path(k, DB_PATH):
                c["big"] = f"/api/cover-upgrade/{k}"
        covers += coverupgrade.own_entries(series_id, number, DB_PATH)   # Marvel.com's print-size variants
        covers += coverupgrade.fandom_entries(series_id, number, DB_PATH)   # the wiki galleries' textless/alternate scans
        return {"covers": covers, "locg_issue_id": locg_issue_id, "source": data.get("source"),
                "selected_ids": sel_ids, "primary_id": primary, "record": data.get("record")}
    except Exception as e:
        raise HTTPException(502, detail=str(e)) from e


@app.get("/api/cover-upgrade/{key:path}")
def get_cover_upgrade(key: str):
    """The bigger stored copy linked to a cover key (kometa/coverupgrade.py) — the
    Variants large view. 404 when nothing is linked."""
    from kometa import coverupgrade, images
    p = coverupgrade.better_path(key, DB_PATH)
    if not p:
        raise HTTPException(404)
    with open(p, "rb") as fh:
        data = fh.read()
    from fastapi.responses import Response
    return Response(content=data, media_type=images._ct(data),
                    headers={"Cache-Control": "public, max-age=86400", "ETag": images.file_etag(p)})


@app.get("/api/cover-store/{key:path}")
def get_cover_store(key: str, w: int | None = None):
    """A stored Marvel.com copy for the Variants tab — whole, or cut down to `w`
    wide for the grid (a 2800px page per tile is a lot of bytes for a thumbnail)."""
    from kometa import coverupgrade, images
    p = coverupgrade.stored_marvel_path(key, DB_PATH)
    if not p:
        raise HTTPException(404)
    with open(p, "rb") as fh:
        data = fh.read()
    if w:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((max(100, min(int(w), 1200)), 10_000))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
    from fastapi.responses import Response
    return Response(content=data, media_type=images._ct(data), headers={"Cache-Control": "public, max-age=86400"})


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


import mimetypes  # noqa: E402
mimetypes.add_type("font/woff2", ".woff2")      # the self-hosted Victor Mono (fonts/) — not application/octet-stream
app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
