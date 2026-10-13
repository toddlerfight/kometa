"""One polite throttle per outside source that has no API contract with us.

PRH's cover CDN, Open Library and ComicBookRoundup (enrichment spec 1i) publish
no rate headers and owe us nothing; the user's ask (2026-10-12) was "use them if
we can — polite rate limiting, not a production rate". So each source module
makes exactly ONE of these at import time and every request it ever sends goes
through it: one lock (never two requests to the same host at once), a minimum
gap, a daily cap counted when sent, and a pause after a refusal or an outage
so a struggling host isn't knocked on again for a while. Same shape as
gcd_client's throttle, without the credentials.
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import date

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "Kometa/1.0 (personal comic library; polite background harvest)"
REFUSED_STATUSES = (403, 429, 503)


# Today's count and any rest survive a restart: kept beside the database in one small
# file, every limiter under its name. Before this a deploy handed every source a fresh
# day — a busy deploy night could send Marvel.com well past its 400.
STATE_FILE = "limiters.json"
_file_lock = threading.Lock()


def _state_path() -> str | None:
    from kometa import db
    folder = os.path.dirname(db.DB_PATH)
    return os.path.join(folder, STATE_FILE) if folder and os.path.isdir(folder) else None


def _read_all(p: str) -> dict:
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class SourceUnavailable(RuntimeError):
    """Switched off, paused, out of today's requests, or the host didn't answer.
    NOT 'not found' — a 404 comes back as a response for the caller to read."""


class Limiter:
    def __init__(self, name: str, min_interval_s: float, daily_cap: int, *,
                 refused_pause_s: float = 3 * 3600, unreachable_pause_s: float = 600,
                 timeout_s: float = 30, user_agent: str = USER_AGENT,
                 refused_statuses: tuple = REFUSED_STATUSES):
        self.name = name
        self.min_interval_s = min_interval_s
        self.daily_cap = daily_cap
        self.refused_pause_s = refused_pause_s
        self.unreachable_pause_s = unreachable_pause_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.refused_statuses = refused_statuses     # S3 answers 403 for a key that isn't there: not a refusal
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._state = {"last": 0.0, "day": "", "count": 0, "paused_until": 0.0}
        self._loaded = False

    def _load(self):
        """Once, on first use: today's count and a rest still running, from the file."""
        if self._loaded:
            return
        self._loaded = True
        p = _state_path()
        saved = _read_all(p).get(self.name) if p else None
        if not isinstance(saved, dict):
            return
        if saved.get("day") == date.today().isoformat():
            self._state.update(day=saved["day"], count=max(self._state["count"], int(saved.get("count") or 0)))
        self._state["paused_until"] = max(self._state["paused_until"], float(saved.get("paused_until") or 0))

    def _save(self):
        p = _state_path()
        if not p:
            return
        with _file_lock:
            try:
                all_ = _read_all(p)
                all_[self.name] = {k: self._state[k] for k in ("day", "count", "paused_until")}
                fd, tmp = tempfile.mkstemp(prefix=".limiters", dir=os.path.dirname(p))
                with os.fdopen(fd, "w") as fh:
                    json.dump(all_, fh)
                os.replace(tmp, p)
            except OSError as e:
                logger.debug(f"limiter state not saved: {e}")

    def _roll_day(self):
        self._load()
        today = date.today().isoformat()
        if self._state["day"] != today:
            self._state.update(day=today, count=0)

    def budget(self) -> dict:
        self._roll_day()
        return {"used": self._state["count"], "cap": self.daily_cap,
                "left": max(0, self.daily_cap - self._state["count"]),
                "paused": self._state["paused_until"] > time.time()}

    def spent(self) -> bool:
        """Out of today's requests or resting — a worker's cheap 'don't start'."""
        b = self.budget()
        return b["left"] <= 0 or b["paused"]

    def rest(self, seconds: float):
        """Stop sending for a while without a refusal to blame — e.g. a page that
        no longer has the shape the worker reads (better idle than mis-record)."""
        with self._lock:
            self._load()
            self._state["paused_until"] = max(self._state["paused_until"], time.time() + seconds)
            self._save()

    def resume(self):
        """Lift a rest early — a fresh pass in Settings means the last refusal is moot."""
        with self._lock:
            self._load()
            self._state["paused_until"] = 0.0
            self._save()

    def reset(self):
        """Tests only: a fresh day, no pause (and nothing read back from a file)."""
        self._state.update(last=0.0, day="", count=0, paused_until=0.0)
        self._loaded = True

    def get(self, url: str, **kw) -> requests.Response:
        """One GET through the one throttle. Returns the response for 2xx/3xx/404
        and the other 4xx the caller may want to read; raises SourceUnavailable
        for refusals (403/429/503 → pause, Retry-After honoured), outages, a
        spent day or an active pause."""
        return self._send("get", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        """One POST through the same throttle (Hardcover's GraphQL), same rules."""
        return self._send("post", url, **kw)

    def _send(self, method: str, url: str, **kw) -> requests.Response:
        headers = {"User-Agent": self.user_agent, **(kw.pop("headers", None) or {})}
        with self._lock:
            self._roll_day()
            if self._state["paused_until"] > time.time():
                raise SourceUnavailable(f"{self.name} paused")
            if self._state["count"] >= self.daily_cap:
                raise SourceUnavailable(f"{self.name}: today's requests are spent")
            wait = self._state["last"] + self.min_interval_s - time.time()
            if wait > 0:
                self.sleep(wait)
            self._state["count"] += 1               # counted when sent: a failed request still costs the day
            try:
                r = getattr(requests, method)(url, headers=headers, timeout=kw.pop("timeout", self.timeout_s), **kw)
            except requests.RequestException as e:
                self._state["paused_until"] = time.time() + self.unreachable_pause_s
                raise SourceUnavailable(f"{self.name} unreachable: {e.__class__.__name__}")
            finally:
                self._state["last"] = time.time()
                self._save()                        # the count (and any pause set below is saved too)
            if r.status_code in self.refused_statuses:
                try:
                    pause = float(r.headers.get("Retry-After") or self.refused_pause_s)
                except (TypeError, ValueError, AttributeError):
                    pause = self.refused_pause_s
                self._state["paused_until"] = time.time() + pause
                self._save()
                logger.info(f"{self.name} refused us ({r.status_code}); resting {pause / 60:.0f} min")
                raise SourceUnavailable(f"{self.name} refused us ({r.status_code})")
            if r.status_code >= 500:
                self._state["paused_until"] = time.time() + self.unreachable_pause_s
                self._save()
                raise SourceUnavailable(f"{self.name} answered {r.status_code}")
            return r


def enabled(key: str, path=None) -> bool:
    """A source's Settings toggle: absent = on (existing installs unchanged)."""
    from kometa import db
    try:
        return db.get_config(path or db.DB_PATH).get(key, "1") != "0"
    except Exception:
        return False
