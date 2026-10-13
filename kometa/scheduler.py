import os
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

TZ = ZoneInfo(os.environ.get("KOMETA_TZ", "Australia/Brisbane"))


def sync_hours() -> list[int]:
    """Sync hours from DB config — the Settings field writes there, and
    db._seed_defaults seeds it from KOMETA_SYNC_HOURS on first boot, so the env
    var still works for fresh deploys. This used to read the env var directly,
    which made the Settings field a placebo: you'd type new hours, the UI would
    nod politely, and the scheduler would keep marching to the compose file.
    Resolved lazily (not at import) so Settings edits are picked up on restart."""
    from kometa import db
    try:
        raw = db.get_config().get("sync_hours", "")
    except Exception:
        raw = ""  # no DB yet (first boot, tests) — env/default carries it
    raw = raw or os.environ.get("KOMETA_SYNC_HOURS", "5,12,17")
    hours = [int(p) for p in (part.strip() for part in raw.split(","))
             if p.isdigit() and 0 <= int(p) <= 23]
    return hours or [5, 12, 17]

# How often to poll SABnzbd for in-flight usenet downloads. Lower = smoother progress
# bar (SAB's the source of truth for %, the UI only sees what we last polled). It's a
# local API and the poll no-ops when nothing's pending, so a tight interval is cheap.
USENET_POLL_SECONDS = int(os.environ.get("KOMETA_USENET_POLL_SECONDS", "5"))


def last_scheduled_sync_utc() -> str:
    """The most recent SYNC_HOURS fire time as a 'YYYY-MM-DD HH:MM:SS' UTC string
    (string-comparable with SQLite datetime('now') stamps). The jobstore is
    in-memory, so a restarted container knows nothing about fires it slept
    through — main's startup catch-up compares this against the last_full_sync
    config stamp to decide whether a scheduled sync was missed."""
    hours = sync_hours()
    if not hours:
        return ""
    now_local = datetime.now(TZ)
    candidates = []
    for day_offset in (0, -1):
        day = now_local + timedelta(days=day_offset)
        for hour in hours:
            slot = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot <= now_local:
                candidates.append(slot)
    return max(candidates).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _as_harvest(fn):
    """The job, run inside interactive.harvesting() — Metron's harvest floor applies
    (kometa/metron_client.py). functools.wraps keeps the name APScheduler logs."""
    import functools
    from kometa.interactive import harvesting

    @functools.wraps(fn)
    def run(*a, **k):
        with harvesting():
            return fn(*a, **k)
    return run


def start_scheduler(sync_all_fn, queue_fn, release_retry_fn, poll_usenet_fn=None, poll_torrent_fn=None,
                    trickle_fn=None):
    # Pollers and the queue get a pool of their own: with ~19 jobs on 10 threads,
    # a long sync plus the trickles sleeping on a reader left the 5-second pollers
    # nowhere to run ("maximum number of running instances reached", all day).
    from apscheduler.executors.pool import ThreadPoolExecutor as _Pool
    scheduler = BackgroundScheduler(timezone=TZ, executors={"default": _Pool(10), "fast": _Pool(3)})

    # misfire_grace_time: apscheduler's default is 1 SECOND — a container
    # restart (i.e. every deploy) straddling a fire time silently DROPS that
    # run, and the pull list doesn't grab until the next window. With an hour
    # of grace + coalesce, a restart-straddled sync fires once as soon as the
    # container is back up instead of vanishing.
    hours = sync_hours()
    for hour in hours:
        scheduler.add_job(
            sync_all_fn,
            # timezone=TZ is NOT optional decoration: a CronTrigger built by hand
            # locks in tzlocal() at construction and IGNORES the scheduler's
            # timezone. In a TZ-less container that's UTC — which had this thing
            # firing "5am" syncs at 3pm Brisbane while the wall clock lied to us.
            CronTrigger(hour=hour, minute=0, timezone=TZ),
            id=f"sync_all_{hour}",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

    # Process download queue every 5 minutes
    scheduler.add_job(
        queue_fn,
        IntervalTrigger(minutes=5),
        id="queue_processor",
        replace_existing=True, executor="fast", coalesce=True,
    )

    # Poll SABnzbd for pending usenet jobs (interval configurable — default 5s)
    if poll_usenet_fn:
        scheduler.add_job(
            poll_usenet_fn,
            IntervalTrigger(seconds=USENET_POLL_SECONDS),
            id="usenet_poller",
            replace_existing=True, executor="fast", coalesce=True,
        )

    # Poll qBittorrent for pending torrent jobs (same cadence as usenet)
    if poll_torrent_fn:
        scheduler.add_job(
            poll_torrent_fn,
            IntervalTrigger(seconds=USENET_POLL_SECONDS),
            id="torrent_poller",
            replace_existing=True, executor="fast", coalesce=True,
        )

    # Release-day retry: 3PM–11PM AEST every 2h on any day with releases
    for hour in (15, 17, 19, 21, 23):
        scheduler.add_job(
            release_retry_fn,
            CronTrigger(hour=hour, minute=0, timezone=TZ),  # same tzlocal() trap as above
            id=f"release_retry_{hour}",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

    # Shelf matcher trickle: one pending series per tick (the tick itself
    # checks daytime + LOCG backoff). Never a batch — see shelf_import.
    if trickle_fn:
        from kometa.shelf_import import TRICKLE_MINUTES
        # First tick one minute after boot, not twenty: an interval job's clock
        # restarts with the container, and a deploy-heavy evening (2026-10-08,
        # ~25 restarts) starved the matcher to two ticks in three hours.
        scheduler.add_job(
            trickle_fn,
            IntervalTrigger(minutes=TRICKLE_MINUTES),
            id="shelf_match_trickle",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(TZ) + timedelta(minutes=1),
        )

    # Related/Suggestions signals: one Metron call per series, a few per tick,
    # stops at the first refusal. The rows render from whatever is cached.
    try:
        from kometa.related import trickle_signals
        scheduler.add_job(trickle_signals, IntervalTrigger(minutes=30), id="related_signals_trickle",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=3))
    except Exception as e:
        logger.warning(f"Related signals trickle not scheduled: {e}")
    # The local catalogue record (kometa/record.py): the backlog fills a few
    # issues per tick, Metron first, stopping at its first refusal. Backfill jobs
    # run as harvest traffic: they stop for the day at Metron's harvest floor,
    # leaving the rest of the allowance to syncs and whatever you're looking at.
    try:
        from kometa.record import trickle as record_trickle, TRICKLE_MINUTES as RECORD_MINUTES
        scheduler.add_job(_as_harvest(record_trickle), IntervalTrigger(minutes=RECORD_MINUTES), id="record_trickle",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=2))
    except Exception as e:
        logger.warning(f"Record trickle not scheduled: {e}")
    try:
        from kometa.record import trades_trickle
        scheduler.add_job(_as_harvest(trades_trickle), IntervalTrigger(minutes=10), id="trades_trickle",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=5))
    except Exception as e:
        logger.warning(f"Trades trickle not scheduled: {e}")
    # Cover art into the record's own store (kometa/images.py): a few dozen a tick.
    try:
        from kometa.images import images_trickle
        scheduler.add_job(images_trickle, IntervalTrigger(minutes=10), id="images_trickle",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=4))
    except Exception as e:
        logger.warning(f"Images trickle not scheduled: {e}")
    # Page 1 and ComicInfo of every owned file into the record's store — disk
    # and CPU only, so a bigger bite per tick (kometa/covers.py).
    try:
        from kometa.covers import covers_trickle
        scheduler.add_job(covers_trickle, IntervalTrigger(minutes=5), id="covers_trickle",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=1))
    except Exception as e:
        logger.warning(f"Covers trickle not scheduled: {e}")
    # Series the first matcher left without a Metron run: asked again, a few a
    # tick, on the one throttle (kometa/rematch.py). Never a second process.
    try:
        from kometa.rematch import rematch_tick
        scheduler.add_job(_as_harvest(rematch_tick), IntervalTrigger(minutes=10), id="metron_rematch",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=6))
    except Exception as e:
        logger.warning(f"Re-match job not scheduled: {e}")
    # Runs matched to Metron after their last sync: their issue lists (and so
    # every issue id) came a week late at best (kometa/harvest.py).
    try:
        from kometa.harvest import issue_ids_tick
        scheduler.add_job(_as_harvest(issue_ids_tick), IntervalTrigger(minutes=10), id="harvest_issue_ids",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=3))
    except Exception as e:
        logger.warning(f"Issue-id harvest not scheduled: {e}")
    # Run status (Ongoing / Completed / …) for the series page's badge: a few
    # Metron series pages a tick, harvest share only (kometa/harvest.py status_tick).
    try:
        from kometa.harvest import status_tick
        scheduler.add_job(_as_harvest(status_tick), IntervalTrigger(minutes=10), id="harvest_run_status",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=7))
    except Exception as e:
        logger.warning(f"Run status harvest not scheduled: {e}")
    # ComicVine's full-size covers: one request per 100 owned issues (kometa/harvest.py, 1f).
    try:
        from kometa.harvest import cv_covers_tick
        scheduler.add_job(cv_covers_tick, IntervalTrigger(minutes=10), id="harvest_cv_covers",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=5))
    except Exception as e:
        logger.warning(f"ComicVine covers harvest not scheduled: {e}")
    # The Grand Comics Database for the runs Metron hasn't got: link a few, then
    # fill their owned issues — GCD's own throttle and daily cap, no Metron
    # traffic, so no harvest wrapper (kometa/harvest.py, 1g). Both no-op without
    # a login or with the Settings toggle off.
    try:
        from kometa.harvest import gcd_link_tick, gcd_fill_tick
        scheduler.add_job(gcd_link_tick, IntervalTrigger(minutes=10), id="harvest_gcd_link",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=7))
        scheduler.add_job(gcd_fill_tick, IntervalTrigger(minutes=10), id="harvest_gcd_fill",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=9))
    except Exception as e:
        logger.warning(f"GCD harvest not scheduled: {e}")
    # The polite extras (1i): PRH's print covers, Open Library's book ratings and
    # covers, ComicBookRoundup's critic/user scores, the Fandom wikis' cover
    # galleries (listed only — downloads have their own switch), Hardcover's book
    # ratings. Each module owns one throttle and a daily cap (Fandom's is
    # fandom_client's, shared with the collected-edition lookups), and each tick
    # no-ops when its Settings toggle is off. No Metron traffic, so no harvest wrapper.
    for mod_name, job_id, offset in (("prh", "harvest_prh", 4), ("openlibrary", "harvest_openlibrary", 6),
                                     ("cbr", "harvest_cbr", 8), ("fandom_covers", "harvest_fandom", 3),
                                     ("hardcover", "harvest_hardcover", 5),
                                     # Marvel.com (1i-e): NIGHT ONLY — the tick no-ops outside marveldc_hours
                                     ("marveldc", "harvest_marveldc", 2)):
        try:
            import importlib
            mod = importlib.import_module(f"kometa.{mod_name}")
            scheduler.add_job(mod.tick, IntervalTrigger(minutes=10), id=job_id,
                              replace_existing=True, coalesce=True, max_instances=1,
                              next_run_time=datetime.now(TZ) + timedelta(minutes=offset))
        except Exception as e:
            logger.warning(f"{mod_name} harvest not scheduled: {e}")
    # ComicBookRoundup's people (kometa/cbr_people.py): ONE page a minute, night only
    # (cbr_people_hours, default 23-7), its own 480-a-day limiter. Outside the window
    # the tick returns after a cached config read, so 960 daytime no-op ticks cost nothing.
    try:
        from kometa.cbr_people import tick as cbr_people_tick
        scheduler.add_job(cbr_people_tick, IntervalTrigger(minutes=1), id="harvest_cbr_people",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=2))
    except Exception as e:
        logger.warning(f"CBR people harvest not scheduled: {e}")
    # Bigger copies of the same cover stand in for the small ones (kometa/coverupgrade.py):
    # links any stored stand-in that predates its link; one cheap query once caught up.
    try:
        from kometa.coverupgrade import backfill as coverupgrade_backfill
        scheduler.add_job(coverupgrade_backfill, IntervalTrigger(minutes=30), id="coverupgrade_backfill",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=1))
    except Exception as e:
        logger.warning(f"Cover-upgrade backfill not scheduled: {e}")
    # ComicInfo into the files (kometa/retag.py): the full run, new arrivals and
    # re-checks — a few files per tick, and nothing at all unless retag_enabled = '1'.
    try:
        from kometa.retag import retag_tick
        scheduler.add_job(retag_tick, IntervalTrigger(minutes=10), id="retag",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=7))
    except Exception as e:
        logger.warning(f"Retag job not scheduled: {e}")
    # Retags held back while their book was open in the reader (kometa/localfirst.py, 5b):
    # onto retag's list once closed. A database move only; empty unless read_local_first ran.
    try:
        from kometa.localfirst import flush_deferred
        scheduler.add_job(flush_deferred, IntervalTrigger(minutes=5), id="retag_deferred",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=6))
    except Exception as e:
        logger.warning(f"Deferred-retag job not scheduled: {e}")
    # Covers go home to the series folders on the NAS as they arrive (kometa/coverhome.py, 2e).
    try:
        from kometa.coverhome import sync_tick as coverhome_tick
        scheduler.add_job(coverhome_tick, IntervalTrigger(minutes=30), id="coverhome_sync",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=9))
    except Exception as e:
        logger.warning(f"Covers home sync not scheduled: {e}")
    # Trades fill what singles can't: collects for a few trades, the coverage
    # stamps, then a few stuck issues looked up — proposals wait in Activity.
    try:
        from kometa.tradefill import trickle as _tradefill_trickle
        scheduler.add_job(_as_harvest(_tradefill_trickle), IntervalTrigger(minutes=10), id="trade_fill",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=4))
    except Exception as e:
        logger.warning(f"Trade fill not scheduled: {e}")
    # The page cache keeps to its cap (KOMETA_PAGE_CACHE_GB, 20 by default) even
    # when nobody reads: what you haven't opened lately goes first.
    try:
        from kometa.reader import trim_cache
        scheduler.add_job(trim_cache, IntervalTrigger(hours=6), id="page_cache_trim",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=5))
    except Exception as e:
        logger.warning(f"Page-cache trim not scheduled: {e}")
    # A Get missing a restart cut off (a deploy mid-run) starts again, once the
    # app has settled. Repeat-safe: owned entries skip, queued issues dedupe.
    try:
        from kometa.listget import resume_get_missing
        scheduler.add_job(resume_get_missing, "date", run_date=datetime.now(TZ) + timedelta(seconds=90),
                          id="listget_resume", replace_existing=True)
    except Exception as e:
        logger.warning(f"Get missing resume not scheduled: {e}")
    # The re-check of the 'no run' series is retired: it re-asked your own
    # Needs-matching answers (kometa/rematch.py recheck_undo gives them back).
    try:
        from kometa.rematch import recheck_undo
        recheck_undo()
    except Exception as e:
        logger.warning(f"Re-check undo failed: {e}")
    # The LOCG top-up queue drains in small bursts while a pass is live.
    try:
        from kometa.topup import scheduled_drain
        scheduler.add_job(scheduled_drain, IntervalTrigger(minutes=15), id="locg_topup_drain",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=6))
    except Exception as e:
        logger.warning(f"LOCG top-up drain not scheduled: {e}")
    # Variants (and details) for owned issues while a LOCG pass is live, one
    # request every three seconds, twenty a tick, never during a sync, and the
    # tick stops at the first refusal (kometa/locg_sweep.py).
    try:
        from kometa.locg_sweep import scheduled_tick as locg_sweep_tick
        scheduler.add_job(locg_sweep_tick, IntervalTrigger(minutes=10), id="locg_sweep",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(minutes=8))
    except Exception as e:
        logger.warning(f"LOCG sweep not scheduled: {e}")
    # The record and the cover store copied to the NAS every night (kometa/backup.py).
    try:
        from kometa.backup import nightly as nightly_backup
        scheduler.add_job(nightly_backup, CronTrigger(hour=3, minute=30, timezone=TZ), id="nightly_backup",
                          replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600)
    except Exception as e:
        logger.warning(f"Nightly backup not scheduled: {e}")
    try:
        from kometa.related import warm_lists, LISTS_TTL_S
        scheduler.add_job(warm_lists, IntervalTrigger(seconds=LISTS_TTL_S - 10), id="related_lists_warm",
                          replace_existing=True, coalesce=True, max_instances=1,
                          next_run_time=datetime.now(TZ) + timedelta(seconds=20))
    except Exception as e:
        logger.warning(f"Related lists warm not scheduled: {e}")

    scheduler.start()
    logger.info(f"Scheduler started — syncing+sweeping at {hours} {TZ.key}, queue every 5min, usenet poll every {USENET_POLL_SECONDS}s, release-day retry daily 15/17/19/21/23 {TZ.key}")
    return scheduler
