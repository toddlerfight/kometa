"""Grand Comics Database — the back catalogue's memory.

Read-only REST API at comics.org/api/ (Django REST Framework). Basic auth with
a free account lifts the limit from 30/hour anonymous to 2,000/day
(gcd-django settings.py: DEFAULT_THROTTLE_RATES). No X-RateLimit headers come
back, so we count for ourselves. This is just the connection for now — the
harvest worker (docs/library-enrichment-spec.md, 1g) builds on it.
"""
import logging

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.comics.org/api/"
USER_AGENT = "Kometa/1.0 (personal comic library)"
REQUEST_TIMEOUT_S = 20
# A known issue (Batman: The Long Halloween #1) — exists for good, cheap to fetch.
_PROBE_ISSUE = 256114


def _creds() -> tuple[str, str] | None:
    from kometa import db
    cfg = db.get_config(db.DB_PATH)
    user, pw = (cfg.get("gcd_user") or "").strip(), cfg.get("gcd_pass") or ""
    return (user, pw) if user and pw else None


def test(user: str | None = None, password: str | None = None) -> tuple[bool, str]:
    """ONE authenticated request. Bad credentials come back 401/403 from DRF's
    BasicAuthentication before the read-only permission is even consulted, so
    a 200 here means the login itself was accepted — not just anonymous access."""
    creds = (user, password) if user and password else _creds()
    if not creds:
        return False, "Not configured"
    try:
        r = requests.get(f"{BASE}issue/{_PROBE_ISSUE}/", params={"format": "json"}, auth=creds,
                         headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as e:
        return False, f"GCD unreachable: {e.__class__.__name__}"
    if r.status_code == 200:
        return True, "Signed in — 2,000 requests a day"
    if r.status_code in (401, 403):
        return False, "GCD refused the username or password"
    if r.status_code == 429:
        return False, "GCD says slow down — try again later"
    return False, f"GCD answered {r.status_code}"
