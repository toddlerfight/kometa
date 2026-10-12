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
import logging
import threading
import time
from datetime import date

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "Kometa/1.0 (personal comic library; polite background harvest)"
REFUSED_STATUSES = (403, 429, 503)


class SourceUnavailable(RuntimeError):
    """Switched off, paused, out of today's requests, or the host didn't answer.
    NOT 'not found' — a 404 comes back as a response for the caller to read."""


class Limiter:
    def __init__(self, name: str, min_interval_s: float, daily_cap: int, *,
                 refused_pause_s: float = 3 * 3600, unreachable_pause_s: float = 600,
                 timeout_s: float = 30, user_agent: str = USER_AGENT):
        self.name = name
        self.min_interval_s = min_interval_s
        self.daily_cap = daily_cap
        self.refused_pause_s = refused_pause_s
        self.unreachable_pause_s = unreachable_pause_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._state = {"last": 0.0, "day": "", "count": 0, "paused_until": 0.0}

    def _roll_day(self):
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

    def reset(self):
        """Tests only: a fresh day, no pause."""
        self._state.update(last=0.0, day="", count=0, paused_until=0.0)

    def get(self, url: str, **kw) -> requests.Response:
        """One GET through the one throttle. Returns the response for 2xx/3xx/404
        and the other 4xx the caller may want to read; raises SourceUnavailable
        for refusals (403/429/503 → pause, Retry-After honoured), outages, a
        spent day or an active pause."""
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
                r = requests.get(url, headers=headers, timeout=kw.pop("timeout", self.timeout_s), **kw)
            except requests.RequestException as e:
                self._state["paused_until"] = time.time() + self.unreachable_pause_s
                raise SourceUnavailable(f"{self.name} unreachable: {e.__class__.__name__}")
            finally:
                self._state["last"] = time.time()
            if r.status_code in REFUSED_STATUSES:
                try:
                    pause = float(r.headers.get("Retry-After") or self.refused_pause_s)
                except (TypeError, ValueError, AttributeError):
                    pause = self.refused_pause_s
                self._state["paused_until"] = time.time() + pause
                logger.info(f"{self.name} refused us ({r.status_code}); resting {pause / 60:.0f} min")
                raise SourceUnavailable(f"{self.name} refused us ({r.status_code})")
            if r.status_code >= 500:
                self._state["paused_until"] = time.time() + self.unreachable_pause_s
                raise SourceUnavailable(f"{self.name} answered {r.status_code}")
            return r


def enabled(key: str, path=None) -> bool:
    """A source's Settings toggle: absent = on (existing installs unchanged)."""
    from kometa import db
    try:
        return db.get_config(path or db.DB_PATH).get(key, "1") != "0"
    except Exception:
        return False
