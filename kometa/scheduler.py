import os
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_raw = os.environ.get("KOMETA_SYNC_HOURS", "5,12,17")
SYNC_HOURS = [int(h.strip()) for h in _raw.split(",")]

TZ = ZoneInfo(os.environ.get("KOMETA_TZ", "Australia/Sydney"))

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
    if not SYNC_HOURS:
        return ""
    now_local = datetime.now(TZ)
    candidates = []
    for day_offset in (0, -1):
        day = now_local + timedelta(days=day_offset)
        for hour in SYNC_HOURS:
            slot = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot <= now_local:
                candidates.append(slot)
    return max(candidates).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def start_scheduler(sync_all_fn, queue_fn, release_retry_fn, poll_usenet_fn=None, poll_torrent_fn=None):
    scheduler = BackgroundScheduler(timezone=TZ)

    # misfire_grace_time: apscheduler's default is 1 SECOND — a container
    # restart (i.e. every deploy) straddling a fire time silently DROPS that
    # run, and the pull list doesn't grab until the next window. With an hour
    # of grace + coalesce, a restart-straddled sync fires once as soon as the
    # container is back up instead of vanishing.
    for hour in SYNC_HOURS:
        scheduler.add_job(
            sync_all_fn,
            CronTrigger(hour=hour, minute=0),
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
            CronTrigger(hour=hour, minute=0),
            id=f"release_retry_{hour}",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

    scheduler.start()
    logger.info(f"Scheduler started — syncing+sweeping at {SYNC_HOURS} AEST, queue every 5min, usenet poll every {USENET_POLL_SECONDS}s, release-day retry daily 15/17/19/21/23 AEST")
    return scheduler
