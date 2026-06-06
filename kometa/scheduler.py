import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_raw = os.environ.get("KOMETA_SYNC_HOURS", "5,12,17")
SYNC_HOURS = [int(h.strip()) for h in _raw.split(",")]


def start_scheduler(sync_all_fn, queue_fn, release_retry_fn, poll_usenet_fn=None):
    scheduler = BackgroundScheduler(timezone="Australia/Sydney")

    for hour in SYNC_HOURS:
        scheduler.add_job(
            sync_all_fn,
            CronTrigger(hour=hour, minute=0),
            id=f"sync_all_{hour}",
            replace_existing=True,
        )

    # Process download queue every 5 minutes
    scheduler.add_job(
        queue_fn,
        IntervalTrigger(minutes=5),
        id="queue_processor",
        replace_existing=True,
    )

    # Poll SABnzbd for pending usenet jobs every 60 seconds
    if poll_usenet_fn:
        scheduler.add_job(
            poll_usenet_fn,
            IntervalTrigger(seconds=60),
            id="usenet_poller",
            replace_existing=True,
        )

    # Release-day retry: 3PM–11PM AEST every 2h on any day with releases
    for hour in (15, 17, 19, 21, 23):
        scheduler.add_job(
            release_retry_fn,
            CronTrigger(hour=hour, minute=0),
            id=f"release_retry_{hour}",
            replace_existing=True,
        )

    scheduler.start()
    logger.info(f"Scheduler started — syncing+sweeping at {SYNC_HOURS} AEST, queue every 5min, usenet poll every 60s, release-day retry daily 15/17/19/21/23 AEST")
    return scheduler
