import os
import logging
import requests

logger = logging.getLogger(__name__)

_COMIC_EXTS = {'.cbz', '.cbr', '.zip', '.rar'}


class SABnzbdClient:
    def __init__(self, url: str, apikey: str):
        self.url = url.rstrip("/")
        self.apikey = apikey
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
            data = self._api(mode="addurl", name=nzb_url, nzbname=nzb_name or "")
            ids = data.get("nzo_ids", [])
            if ids:
                logger.info(f"SABnzbd: submitted {nzb_url[:80]} → nzo_id={ids[0]}")
                return ids[0]
            logger.warning(f"SABnzbd addurl returned no nzo_id: {data}")
            return None
        except Exception as e:
            logger.warning(f"SABnzbd addurl failed: {e}")
            return None

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
            return {"status": "queued", "pct": pct}

        slot = self.get_history_slot(nzo_id)
        if slot:
            status = (slot.get("status") or "").lower()
            if status == "completed":
                return {"status": "completed", "storage": slot.get("storage", "")}
            return {"status": "failed", "error": slot.get("fail_message") or slot.get("status", "unknown")}

        return {"status": "unknown"}


def find_comic_in_dir(directory: str) -> str | None:
    """Walk directory and return path to the first comic file found."""
    if not directory or not os.path.isdir(directory):
        return None
    for root, _, files in os.walk(directory):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _COMIC_EXTS:
                return os.path.join(root, f)
    return None


def find_comics_in_dir(directory: str) -> list[str]:
    """Return all comic files under directory, sorted."""
    out = []
    if not directory or not os.path.isdir(directory):
        return out
    for root, _, files in os.walk(directory):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _COMIC_EXTS:
                out.append(os.path.join(root, f))
    return out
