"""Metron — the primary metadata source (docs/reader-spec.md, 2026-10-08).

A documented API built for tools like Kometa, unlike LOCG (which we read
unofficially and which started refusing us). Limits, from its own headers:
a burst of 20 and 5,000 a day. We stay well under both — one request at a
time, spaced, and a refusal (429) honours Retry-After for EVERY caller.
"""
import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE = "https://metron.cloud/api/"
USER_AGENT = "Kometa/1.0 (personal pull-list tool)"
MIN_INTERVAL_S = 3.5            # ~17/min, under the burst limit of 20
DAILY_FLOOR = 200               # stop for the day with this many left of the 5,000

_lock = threading.Lock()
_state = {"last": 0.0, "paused_until": 0.0}


class MetronUnavailable(RuntimeError):
    """Not configured, paused, or Metron didn't answer. NOT 'no results'."""


def _creds():
    from kometa import db
    try:
        cfg = db.get_config(db.DB_PATH)
    except Exception:
        return None                    # no readable config = not configured
    if cfg.get("metron_enabled", "1") == "0":
        return None
    u, p = cfg.get("metron_user"), cfg.get("metron_pass")
    return (u, p) if u and p else None


def configured() -> bool:
    return _creds() is not None


def _get(path: str, **params) -> dict:
    creds = _creds()
    if not creds:
        raise MetronUnavailable("Metron isn't configured")
    auth = "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with _lock:                                    # one request at a time, spaced
        now = time.time()
        if now < _state["paused_until"]:
            raise MetronUnavailable("Metron asked us to wait")
        wait = _state["last"] + MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.time()
        req = urllib.request.Request(url, headers={"Authorization": auth, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
                left = r.headers.get("X-Ratelimit-Sustained-Remaining")
                if left is not None and int(left) < DAILY_FLOOR:
                    reset = float(r.headers.get("X-Ratelimit-Sustained-Reset") or time.time() + 3600)
                    _state["paused_until"] = reset
                    logger.warning(f"Metron: {left} requests left today — pausing until its reset")
                return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = float(e.headers.get("Retry-After") or 60)
                _state["paused_until"] = time.time() + retry
                logger.warning(f"Metron rate-limited us — waiting {int(retry)}s")
            raise MetronUnavailable(f"Metron answered {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise MetronUnavailable(f"Metron unreachable: {e}") from e


def _all_pages(path: str, max_pages: int = 10, **params) -> list[dict]:
    out, page = [], 1
    while page <= max_pages:
        body = _get(path, page=page, **params)
        out.extend(body.get("results", []))
        if not body.get("next"):
            break
        page += 1
    return out


def title_variants(title: str) -> list[str]:
    """Folders say 'Hellblazer - Bad Blood'; the catalogue says 'Hellblazer: Bad
    Blood'. Metron's name filter is literal, so ask both ways."""
    t = re.sub(r"\(\d{4}\)", "", title).strip()
    colon = re.sub(r"\s*-\s+", ": ", t, count=1)
    colon = re.sub(r"(\w)- ", r"\1: ", colon, count=1)
    return [t] if colon == t else [t, colon]


def search_series(title: str) -> list[dict]:
    """Normalised rows: {id, title, publisher, year, cv_id, issue_count}.
    Raises MetronUnavailable — never returns [] for 'didn't answer'."""
    seen, rows = set(), []
    for q in title_variants(title):
        for r in _get("series/", name=q).get("results", []):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            pub = r.get("publisher")
            rows.append({
                "id": r["id"],
                "title": re.sub(r"\s*\(\d{4}\)\s*$", "", r.get("series") or ""),
                "publisher": pub.get("name") if isinstance(pub, dict) else pub,
                "year": r.get("year_began"),
                "cv_id": r.get("cv_id"),
                "issue_count": r.get("issue_count"),
            })
    return rows


def _num(n) -> float | None:
    try:
        return float(n)
    except (TypeError, ValueError):
        return None


def series_issues(series_id: int) -> list[dict]:
    """[{number, store_date, image, metron_issue_id}] — the issue list Kometa
    used to take from LOCG."""
    out = []
    for i in _all_pages("issue/", series_id=series_id):
        n = _num(i.get("number"))
        if n is None:
            continue
        out.append({"number": n, "store_date": i.get("store_date"),
                    "image": i.get("image"), "metron_issue_id": i.get("id")})
    return out


# Metron credits the whole masthead — President, Publisher, Chief Creative
# Officer. Jim Lee did not draw your issue of Absolute Batman. Off the list.
_MASTHEAD_ROLES = {"president", "publisher", "chief creative officer", "editor in chief",
                   "executive editor", "group editor", "senior editor", "associate editor",
                   "assistant editor", "editor"}


def issue_detail(metron_issue_id: int) -> dict:
    """Everything the issue modal shows, from one request: description, credits
    and the covers (main + variants, with images). Same shape the LOCG scrape
    produced, so the Details and Variants tabs don't care which source answered.

    Variants carry no id on Metron — the image filename (a UUID) stands in, and
    `large` holds the real URL so the injector never has to guess it."""
    d = _get(f"issue/{int(metron_issue_id)}/")
    credits = []
    for c in d.get("credits") or []:
        for role in c.get("role") or [{"name": "Other"}]:
            name = role.get("name") or "Other"
            if name.lower() in _MASTHEAD_ROLES:
                continue
            credits.append({"role": name, "name": c.get("creator"), "people_id": None,
                            "metron_creator_id": c.get("id")})
    covers = []
    if d.get("image"):
        covers.append({"id": f"m{d['id']}", "name": "Cover A (Main)", "thumb": d["image"], "large": d["image"]})
    seen = set()
    for v in d.get("variants") or []:
        img = v.get("image")
        if not img:
            continue
        vid = re.sub(r"\W", "", img.rsplit("/", 1)[-1].rsplit(".", 1)[0]) or f"v{len(covers)}"
        if vid in seen:
            continue
        seen.add(vid)
        covers.append({"id": vid, "name": v.get("name") or f"Variant {len(covers)}", "thumb": img, "large": img})
    return {
        "desc": d.get("desc") or "",
        "credits": credits,
        "covers": covers,
        "store_date": d.get("store_date"),
        "foc_date": d.get("foc_date"),
        "arcs": [a.get("name") for a in d.get("arcs") or [] if isinstance(a, dict)],
        "source": "metron",
    }


def releases_between(after: str, before: str) -> list[dict]:
    """Every issue Metron has with a store date in [after, before] — the weekly
    what's-new check in a handful of requests."""
    out = []
    for i in _all_pages("issue/", max_pages=20, store_date_range_after=after, store_date_range_before=before):
        s = i.get("series") or {}
        out.append({"metron_series_id": s.get("id") if isinstance(s, dict) else None,
                    "number": _num(i.get("number")), "store_date": i.get("store_date")})
    return out


def test() -> tuple[bool, str]:
    try:
        _get("publisher/", name="Image")
        return True, "Connected"
    except MetronUnavailable as e:
        return False, str(e)
