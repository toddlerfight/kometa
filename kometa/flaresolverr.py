"""FlareSolverr client: a service on the NAS (:8191) that returns the cookie and
User-Agent needed for a domain that otherwise refuses a plain request. Kometa
then makes the real request itself. Used for GetComics' main host
(fs2.comicfiles.ru), which 403s plain requests. Off unless config
`flaresolverr_url` is set.

One result per domain, cached until the cookie's own expiry, behind a lock so
queued downloads for the same domain share a single ~20s call.
"""
import logging
import threading
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[str, dict, float]] = {}      # domain -> (ua, cookies, expires_at)
_lock = threading.Lock()
_DEFAULT_TTL = 30 * 60


def _base() -> str:
    try:
        from kometa import db
        return (db.get_config(db.DB_PATH).get("flaresolverr_url") or "").strip().rstrip("/")
    except Exception:
        return ""


def enabled() -> bool:
    return bool(_base())


def is_challenge(resp) -> bool:
    """A refusal FlareSolverr can handle, not a real 403 from the file host."""
    if resp is None or resp.status_code not in (403, 503):
        return False
    h = resp.headers
    return h.get("cf-mitigated") == "challenge" or "cloudflare" in (h.get("server") or "").lower()


def clearance(url: str) -> tuple[str, dict] | None:
    """(user_agent, cookies) for url's domain, or None."""
    base = _base()
    if not base:
        return None
    p = urlparse(url)
    dom = p.netloc
    with _lock:
        hit = _cache.get(dom)
        if hit and hit[2] > time.time():
            return hit[0], hit[1]
        t0 = time.time()
        try:
            data = requests.post(f"{base}/v1", json={"cmd": "request.get", "url": f"{p.scheme}://{dom}/",
                                                     "maxTimeout": 60000}, timeout=90).json()
        except Exception as e:
            logger.warning(f"FlareSolverr: unreachable at {base} ({e})")
            return None
        sol = data.get("solution") or {}
        cookies = {c["name"]: c["value"] for c in sol.get("cookies", [])}
        if data.get("status") != "ok" or "cf_clearance" not in cookies:
            logger.warning(f"FlareSolverr: no clearance for {dom} ({data.get('message')})")
            return None
        exp = next((c.get("expiry") for c in sol["cookies"] if c["name"] == "cf_clearance"), None)
        expires = min(exp - 60, time.time() + 6 * 3600) if exp else time.time() + _DEFAULT_TTL
        _cache[dom] = (sol.get("userAgent", ""), cookies, expires)
        logger.info(f"FlareSolverr: cleared {dom} in {time.time() - t0:.0f}s")
        return _cache[dom][0], cookies


def headers_for(url: str, base_headers: dict) -> dict | None:
    """base_headers wearing the clearance for url's domain, or None if there's none to be had."""
    got = clearance(url)
    if not got:
        return None
    ua, cookies = got
    return {**base_headers, "User-Agent": ua, "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def forget(url: str) -> None:
    _cache.pop(urlparse(url).netloc, None)


def test(base: str) -> tuple[bool, str]:
    """For the Settings card: is FlareSolverr answering at base?"""
    try:
        j = requests.get(base.rstrip("/") + "/", timeout=8).json()
        return True, f"FlareSolverr {j.get('version', '?')}"
    except Exception as e:
        return False, str(e)[:120]
