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


def start_scheduler(sync_all_fn, queue_fn, release_retry_fn, poll_usenet_fn=None, poll_torrent_fn=None,
                    trickle_fn=None):
    scheduler = BackgroundScheduler(timezone=TZ)

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
        replace_existing=True,
    )

    # Poll SABnzbd for pending usenet jobs (interval configurable — default 5s)
    if poll_usenet_fn:
        scheduler.add_job(
            poll_usenet_fn,
            IntervalTrigger(seconds=USENET_POLL_SECONDS),
            id="usenet_poller",
            replace_existing=True,
        )

    # Poll qBittorrent for pending torrent jobs (same cadence as usenet)
    if poll_torrent_fn:
        scheduler.add_job(
            poll_torrent_fn,
            IntervalTrigger(seconds=USENET_POLL_SECONDS),
            id="torrent_poller",
            replace_existing=True,
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
