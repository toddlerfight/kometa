import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_raw = os.environ.get("KOMETA_SYNC_HOURS", "5,12,17")
SYNC_HOURS = [int(h.strip()) for h in _raw.split(",")]


def start_scheduler(sync_all_fn, queue_fn, sweep_fn):
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

    # Weekly sweep: queue missing issues for monitored series (Mon 03:00 AEST)
    scheduler.add_job(
        sweep_fn,
        CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="missing_sweep",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started — syncing at {SYNC_HOURS} AEST, queue every 5min, sweep Mon 03:00")
    return scheduler
