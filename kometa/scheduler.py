import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# e.g. KOMETA_SYNC_HOURS=5,12,17
_raw = os.environ.get("KOMETA_SYNC_HOURS", "5,12,17")
SYNC_HOURS = [int(h.strip()) for h in _raw.split(",")]


def start_scheduler(sync_all_fn):
    scheduler = BackgroundScheduler(timezone="Australia/Sydney")
    for hour in SYNC_HOURS:
        scheduler.add_job(
            sync_all_fn,
            CronTrigger(hour=hour, minute=0),
            id=f"sync_all_{hour}",
            replace_existing=True,
        )
    scheduler.start()
    logger.info(f"Scheduler started — syncing at {SYNC_HOURS} AEST")
    return scheduler
