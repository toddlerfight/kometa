import os
import logging
import requests

from kometa.naming import PIPELINE_EXTS

logger = logging.getLogger(__name__)


class SABnzbdClient:
    # The SAB is shared with the movie stack, which submits at High. A 40 MB comic
    # queued behind a 30 GB 4K remux waits an hour for nothing (DIE: Loaded,
    # 2026-10-10). So a comic goes in at High AND to the top of the queue.
    def __init__(self, url: str, apikey: str, priority: int = 1, jump_queue: bool = True):
        self.url = url.rstrip("/")
        self.apikey = apikey
        self.priority = priority
        self.jump_queue = jump_queue
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"

    def _api(self, **params) -> dict:
        r = self.session.get(
            f"{self.url}/api",
            params={"apikey": self.apikey, "output": "json", **params},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def add_nzb_url(self, nzb_url: str, nzb_name: str = "") -> str | None:
        """Submit NZB URL. Returns nzo_id or None."""
        try:
            data = self._api(mode="addurl", name=nzb_url, nzbname=nzb_name or "", priority=self.priority)
            ids = data.get("nzo_ids", [])
            if ids:
                logger.info(f"SABnzbd: submitted {nzb_url[:80]} → nzo_id={ids[0]}")
                if self.jump_queue:
                    try:
                        self._api(mode="switch", value=ids[0], value2=0)      # front of the line
                    except Exception as e:
                        logger.info(f"SABnzbd: couldn't move {ids[0]} to the top: {e}")
                return ids[0]
            logger.warning(f"SABnzbd addurl returned no nzo_id: {data}")
            return None
        except Exception as e:
            logger.warning(f"SABnzbd addurl failed: {e}")
            return None

    def queue_paused(self) -> bool:
        """The whole queue paused by a person — nothing is stalled then, it's held."""
        try:
            return bool(self._api(mode="queue", limit=0).get("queue", {}).get("paused"))
        except Exception:
            return False

    def delete_job(self, nzo_id: str) -> bool:
        """Drop a job from the queue (or history) and its partial files."""
        ok = False
        for mode in ("queue", "history"):
            try:
                ok = bool(self._api(mode=mode, name="delete", value=nzo_id, del_files=1).get("status")) or ok
            except Exception as e:
                logger.info(f"SABnzbd delete ({mode}) {nzo_id}: {e}")
        return ok

    def get_queue_slot(self, nzo_id: str) -> dict | None:
        """Check active queue for a job. Returns slot dict or None if not present."""
        try:
            data = self._api(mode="queue")
            slots = data.get("queue", {}).get("slots", [])
            for slot in slots:
                if slot.get("nzo_id") == nzo_id:
                    return slot
        except Exception as e:
            logger.warning(f"SABnzbd queue check failed: {e}")
        return None

    def get_history_slot(self, nzo_id: str) -> dict | None:
        """Check history for a completed/failed job. Returns slot dict or None."""
        try:
            data = self._api(mode="history", limit=100)
            slots = data.get("history", {}).get("slots", [])
            for slot in slots:
                if slot.get("nzo_id") == nzo_id:
                    return slot
        except Exception as e:
            logger.warning(f"SABnzbd history check failed: {e}")
        return None

    def poll_job(self, nzo_id: str) -> dict:
        """
        Poll a job. Returns:
          {"status": "queued",     "pct": float}           — in SABnzbd queue
          {"status": "completed",  "storage": str}          — finished, storage = download path
          {"status": "failed",     "error": str}            — failed in SABnzbd
          {"status": "unknown"}                             — not found in queue or history
        """
        slot = self.get_queue_slot(nzo_id)
        if slot:
            try:
                pct = float(slot.get("percentage", 0))
            except (ValueError, TypeError):
                pct = 0.0
            try:
                mbleft = float(slot.get("mbleft", 0) or 0)
            except (ValueError, TypeError):
                mbleft = None
            return {"status": "queued", "pct": pct, "mbleft": mbleft, "sab_status": slot.get("status") or ""}

        slot = self.get_history_slot(nzo_id)
        if slot:
            status = (slot.get("status") or "").lower()
            if status == "completed":
                return {"status": "completed", "storage": slot.get("storage", "")}
            if status == "failed":
                return {"status": "failed", "error": slot.get("fail_message") or "SABnzbd reported failed"}
            # Everything else in history is a TRANSIENT post-processing stage —
            # Extracting / Verifying / Repairing / Moving / Running-script / Fetching.
            # The download's done; SAB is just unpacking. Treating these as terminal
            # failures is the "FAILED · Extracting" lie — a pure poll-timing race that
            # bails seconds before SAB flips the slot to Completed. Keep polling.
            return {"status": "queued", "pct": 100.0}

        return {"status": "unknown"}


def find_comics_in_dir(directory: str) -> list[str]:
    """Return all comic files under directory, sorted."""
    out: list[str] = []
    if not directory or not os.path.isdir(directory):
        return out
    for root, _, files in os.walk(directory):
        out.extend(
            os.path.join(root, f)
            for f in sorted(files)
            if os.path.splitext(f)[1].lower() in PIPELINE_EXTS
        )
    return out
