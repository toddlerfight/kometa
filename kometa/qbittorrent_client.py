import logging
import re
import requests

logger = logging.getLogger(__name__)

# qBittorrent torrent states (WebAPI v2.x). These are the ones we branch on; the
# rest (downloading, stalledDL, metaDL, queuedDL, checkingDL, forcedDL, moving,
# checkingUP) all mean "not settled yet" → keep polling.
_DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}
_FAIL_STATES = {"error", "missingFiles"}


def infohash_from_magnet(magnet: str) -> str | None:
    """Pull the btih infohash out of a magnet URI, lowercased. This is our handle
    on the torrent — qBit's /torrents/add returns only 'Ok.', not the hash, so we
    derive it from the magnet ourselves and poll by it."""
    m = re.search(r"xt=urn:btih:([0-9A-Fa-f]{40}|[A-Za-z2-7]{32})", magnet)
    return m.group(1).lower() if m else None


class QBittorrentClient:
    """Thin qBittorrent WebAPI v2 client. Mirrors SABnzbdClient's surface so the
    torrent acquisition branch and poller stay symmetric with the usenet ones.

    Reach it from the NAS host / Kometa container via the LAN IP (e.g.
    http://$NAS_HOST:8090) — qBit's WebUI rejects the Tailscale hostname and
    requires auth even on localhost. Use a dedicated category ('kometa') so our
    torrents never collide with the Sonarr/Radarr torrents sharing this client.
    """

    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"
        self.session.headers["Referer"] = self.url  # qBit CSRF/referer check
        self._authed = False

    def _login(self) -> bool:
        try:
            r = self.session.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=15,
            )
            # qBit returns 200 "Ok." OR 204 No Content on success (and sets the
            # QBT_SID cookie either way); only bad creds give 200 "Fails.".
            ok = r.ok and "fail" not in r.text.lower()
            self._authed = ok
            if not ok:
                logger.warning(f"qBittorrent login failed: {r.status_code} {r.text[:40]}")
            return ok
        except Exception as e:
            logger.warning(f"qBittorrent login error: {e}")
            return False

    def _req(self, method: str, path: str, **kw):
        """Auth-aware request. Lazily logs in, and re-logs once on a 403 so an
        expired SID cookie heals itself instead of failing the whole grab."""
        if not self._authed and not self._login():
            return None
        try:
            r = self.session.request(method, f"{self.url}{path}", timeout=20, **kw)
            if r.status_code == 403 and self._login():
                r = self.session.request(method, f"{self.url}{path}", timeout=20, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            logger.warning(f"qBittorrent {method} {path} failed: {e}")
            return None

    def test(self) -> tuple[bool, str]:
        """Verify the WebUI is reachable and the creds authenticate. Returns
        (ok, detail) — detail is the qBit version on success, else the reason."""
        if not self._login():
            return False, "Login failed — check URL, username, and password"
        r = self._req("GET", "/api/v2/app/version")
        if r is None:
            return False, "Authenticated but /app/version did not respond"
        return True, r.text.strip()

    def _hashes_in_category(self, category: str) -> set[str]:
        r = self._req("GET", "/api/v2/torrents/info", params={"category": category})
        return {str(t.get("hash", "")).lower() for t in r.json()} if r is not None else set()

    def add_torrent(self, source: str, category: str = "kometa",
                    savepath: str | None = None, paused: bool = False) -> str | None:
        """Add a magnet OR a .torrent URL. Returns the infohash (our poll handle).
        Magnet → hash derived directly. URL (many indexers give no magnet/infoHash)
        → snapshot the category's hashes, add, then diff to find the newcomer."""
        import time
        is_magnet = source.startswith("magnet:")
        pre = set() if is_magnet else self._hashes_in_category(category)
        data = {"urls": source, "category": category}
        if savepath:
            data["savepath"] = savepath
        if paused:
            data["paused"] = "true"
            data["stopped"] = "true"  # qBit 5.x renamed paused→stopped; send both
        r = self._req("POST", "/api/v2/torrents/add", data=data)
        if r is None:
            return None
        if is_magnet:
            ih = infohash_from_magnet(source)
            logger.info(f"qBittorrent: added magnet {ih} (category={category})")
            return ih
        for _ in range(12):  # URL fetch + registration is usually 1-3s
            time.sleep(1)
            new = self._hashes_in_category(category) - pre
            if new:
                ih = sorted(new)[0]
                logger.info(f"qBittorrent: added .torrent url → {ih} (category={category})")
                return ih
        logger.warning("qBittorrent: .torrent url added but new hash never appeared")
        return None

    def poll_job(self, infohash: str) -> dict:
        """Poll a torrent. Same contract shape as SABnzbdClient.poll_job:
          {"status": "downloading", "pct": float, "seeders": int, "state": str}
          {"status": "completed",   "content_path": str}
          {"status": "failed",      "error": str}
          {"status": "unknown"}
        Completion keys off the seeding states (files settled + in place), not bare
        progress>=1.0, so we never import mid-move/mid-recheck.
        """
        r = self._req("GET", "/api/v2/torrents/info", params={"hashes": infohash})
        if r is None:
            return {"status": "unknown"}
        arr = r.json()
        if not arr:
            return {"status": "unknown"}
        t = arr[0]
        state = t.get("state", "")
        if state in _FAIL_STATES:
            return {"status": "failed", "error": f"qBittorrent state: {state}"}
        if state in _DONE_STATES:
            return {"status": "completed", "content_path": t.get("content_path", "")}
        return {
            "status": "downloading",
            "pct": round(float(t.get("progress", 0) or 0) * 100, 1),
            "seeders": int(t.get("num_complete", 0) or 0),
            "state": state,
        }

