"""Grand Comics Database — the back catalogue's memory.

Read-only REST API at comics.org/api/ (Django REST Framework). Basic auth with
a free account lifts the limit from 30/hour anonymous to 2,000/day
(gcd-django settings.py: DEFAULT_THROTTLE_RATES). No X-RateLimit headers come
back, so we count for ourselves: one client, one lock, a gap between requests,
and a daily cap below the account's 2,000 (docs/library-enrichment-spec.md, 1g).
"""
import logging
import re
import threading
import time
import urllib.parse
from datetime import date

import requests

from kometa import health

logger = logging.getLogger(__name__)

BASE = "https://www.comics.org/api/"
USER_AGENT = "Kometa/1.0 (personal comic library)"
REQUEST_TIMEOUT_S = 20
MIN_INTERVAL_S = 3.0            # process-wide gap; the account allows 2,000/day, nothing per minute is published
GCD_DAILY_CAP = 1800            # stop for the day here: the rest of the 2,000 is slack for the Settings test
UNREACHABLE_PAUSE_S = 300       # a connection failure: everyone fails fast this long
REFUSED_PAUSE_S = 3600          # 401/403/429: an hour off (no Retry-After seen in practice, honoured if sent)
SEARCH_PAGES = 2                # 'Tintin' is 145 series; past two pages it's not a confident match anyway
# A known issue (Batman: The Long Halloween #1) — exists for good, cheap to fetch.
_PROBE_ISSUE = 256114

_lock = threading.Lock()
_state = {"last": 0.0, "day": "", "count": 0, "paused_until": 0.0}


class GCDUnavailable(RuntimeError):
    """Not configured, paused, out of today's requests, or GCD didn't answer.
    NOT 'no results'."""


def _creds() -> tuple[str, str] | None:
    from kometa import db
    cfg = db.get_config(db.DB_PATH)
    user, pw = (cfg.get("gcd_user") or "").strip(), cfg.get("gcd_pass") or ""
    return (user, pw) if user and pw else None


def configured() -> bool:
    """A login, and the Settings toggle on."""
    from kometa import db
    try:
        cfg = db.get_config(db.DB_PATH)
    except Exception:
        return False
    return cfg.get("gcd_enabled", "1") != "0" and _creds() is not None


def _roll_day():
    today = date.today().isoformat()
    if _state["day"] != today:
        _state.update(day=today, count=0)


def budget() -> dict:
    _roll_day()
    return {"used": _state["count"], "cap": GCD_DAILY_CAP, "left": max(0, GCD_DAILY_CAP - _state["count"]),
            "paused": _state["paused_until"] > time.time()}


def _get(path: str, **params):
    """One request through the one throttle. Returns parsed JSON, or None for a
    404 (the thing isn't there). Raises GCDUnavailable for everything else."""
    creds = _creds()
    if not creds:
        raise GCDUnavailable("GCD not configured")
    with _lock:
        _roll_day()
        if _state["paused_until"] > time.time():
            raise GCDUnavailable("GCD paused")
        if _state["count"] >= GCD_DAILY_CAP:
            raise GCDUnavailable("GCD: today's requests are spent")
        wait = _state["last"] + MIN_INTERVAL_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _state["count"] += 1                       # counted when sent: a failed request still counts against the day
        try:
            r = requests.get(BASE + path.lstrip("/"), params={"format": "json", **params}, auth=creds,
                             headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            _state["paused_until"] = time.time() + UNREACHABLE_PAUSE_S
            health.record("GCD", "error", e.__class__.__name__)
            raise GCDUnavailable(f"GCD unreachable: {e.__class__.__name__}")
        finally:
            _state["last"] = time.time()
        health.record("GCD", "ok" if r.status_code in (200, 404) else "refused" if r.status_code in (401, 403, 429) else "error",
                      str(r.status_code))
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403, 429):
            try:
                pause = float(r.headers.get("Retry-After") or REFUSED_PAUSE_S)
            except (TypeError, ValueError, AttributeError):
                pause = REFUSED_PAUSE_S
            _state["paused_until"] = time.time() + pause
            raise GCDUnavailable(f"GCD refused us ({r.status_code})")
        raise GCDUnavailable(f"GCD answered {r.status_code}")


_ID_RE = re.compile(r"/(\d+)/?(?:\?|$)")


def _id_from_url(url) -> int | None:
    m = _ID_RE.search(str(url or ""))
    return int(m.group(1)) if m else None


def _series_row(d: dict) -> dict:
    """A search hit or series detail, cut down to what matching and the
    suggestions need."""
    return {"gcd_id": _id_from_url(d.get("api_url")), "name": d.get("name") or "",
            "year_began": d.get("year_began"), "year_ended": d.get("year_ended"),
            "issues": len(d.get("issue_descriptors") or []) or len(d.get("active_issues") or []),
            "language": (d.get("language") or "").lower(), "country": (d.get("country") or "").lower(),
            "publishing_format": d.get("publishing_format") or "",
            "publisher_id": _id_from_url(d.get("publisher"))}


def search_series(name: str, year: int | None = None) -> list[dict]:
    """GCD's series-by-name search (a contains match), first SEARCH_PAGES pages."""
    q = urllib.parse.quote(name.strip(), safe="")
    path = f"series/name/{q}/" + (f"year/{int(year)}/" if year else "")
    out = []
    for page in range(1, SEARCH_PAGES + 1):
        d = _get(path, **({"page": page} if page > 1 else {}))
        if not d:
            break
        out += [_series_row(r) for r in d.get("results") or []]
        if not d.get("next"):
            break
    return out


_NUM_RE = re.compile(r"^\s*#?(\d+)(?:\s*/\s*(\d+)|\.(\d+))?(?=\s|$|\s*-|\s*\[)")


def parse_number(descriptor: str | None) -> float | None:
    """'368 - King Ottokar's Sceptre' → 368.0; '1/2' → 0.5; '[nn]' → None."""
    m = _NUM_RE.match(descriptor or "")
    if not m:
        return None
    if m.group(2):
        return int(m.group(1)) / int(m.group(2)) if int(m.group(2)) else None
    return float(f"{m.group(1)}.{m.group(3)}") if m.group(3) else float(m.group(1))


def _descriptor_title(descriptor: str) -> str | None:
    parts = (descriptor or "").split(" - ", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


_publisher_names: dict[int, str] = {}


def publisher_name(gcd_publisher_id: int) -> str | None:
    """A publisher's name — one request per publisher, ever (per process)."""
    pid = int(gcd_publisher_id)
    if pid not in _publisher_names:
        d = _get(f"publisher/{pid}/")
        _publisher_names[pid] = (d or {}).get("name") or ""
    return _publisher_names[pid] or None


def series_detail(gcd_series_id: int) -> dict | None:
    """The series and its issue list: active_issues and issue_descriptors run
    side by side, one entry per issue."""
    d = _get(f"series/{int(gcd_series_id)}/")
    if not d:
        return None
    row = _series_row(d)
    row["gcd_id"] = row["gcd_id"] or int(gcd_series_id)
    descs = d.get("issue_descriptors") or []
    row["issues_list"] = [{"gcd_issue_id": _id_from_url(url),
                           "number": parse_number(descs[i] if i < len(descs) else ""),
                           "title": _descriptor_title(descs[i] if i < len(descs) else "")}
                          for i, url in enumerate(d.get("active_issues") or []) if _id_from_url(url)]
    return row


# --- one issue, into the record's shape -------------------------------------------
_FIELD_ROLES = (("script", "Writer"), ("pencils", "Penciller"), ("inks", "Inker"),
                ("colors", "Colorist"), ("letters", "Letterer"), ("editing", "Editor"))
_COVER_FIELDS = ("pencils", "inks", "colors")
# a parenthetical that changes the role; anything else ('signed', 'painted', '?') is a note
_PAREN_ROLES = (("editor", "Editor"), ("plot", "Plot"), ("script", "Writer"), ("dialog", "Writer"),
                ("translat", "Translator"), ("pencil", "Penciller"), ("ink", "Inker"),
                ("colo", "Colorist"), ("letter", "Letterer"))
# masthead, not the people who made this comic (metron_client drops the same)
_MASTHEAD = re.compile(r"chief|publisher|executive|managing|group|senior|associate|assistant|consulting", re.I)
_NOT_A_NAME = {"", "?", "none", "various", "unknown", "typeset", "uncredited", "n/a"}


def _people(text: str | None, role: str) -> list[dict]:
    """GCD credit text → [{role, name}]. 'Tim Sale; Mark Chiarello (editor)',
    'Hergé [as Herge]', 'Stan Lee (plot)', 'Jack Kirby ?'."""
    out = []
    for part in re.split(r";", text or ""):
        notes = re.findall(r"\(([^)]*)\)", part)
        name = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", part).strip().rstrip("?").strip(" .,")
        if name.lower() in _NOT_A_NAME:
            continue
        r = role
        for note in notes:
            if _MASTHEAD.search(note):
                r = None
                break
            hit = next((label for key, label in _PAREN_ROLES if key in note.lower()), None)
            if hit:
                r = hit
                break
        if r:
            out.append({"role": r, "name": name, "people_id": None})
    return out


def _split_list(text: str | None) -> list[str]:
    """'Batman [Bruce Wayne]; Villains: Joker; Two-Face (cameo)' → names."""
    out = []
    for part in re.split(r"[;\n]", text or ""):
        part = re.sub(r"^\s*[A-Za-z][\w /'-]{0,30}:\s*", "", part)          # a group label: 'Villains:'
        part = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", part).strip(" .,?")
        if part and part.lower() not in _NOT_A_NAME:
            out.append(part)
    return out


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _date(raw: str | None) -> str | None:
    """'1959-00-00' → '1959-01-01'; '1986-05' → '1986-05-01'; '' → None."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", (raw or "").strip())
    if not m or m.group(1) == "0000":
        return None
    mm = m.group(2) if m.group(2) and m.group(2) != "00" else "01"
    dd = m.group(3) if m.group(3) and m.group(3) != "00" else "01"
    return f"{m.group(1)}-{mm}-{dd}"


def _int(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n or None


def parse_issue(d: dict, gcd_issue_id: int | None = None) -> dict:
    """GCD's issue → the record shape write_issue takes (metron_client.issue_detail's)."""
    from kometa.metron_client import _ROLE_ORDER
    stories = d.get("story_set") or []
    credits = []
    for s in stories:
        if (s.get("type") or "").lower() == "cover":
            for f in _COVER_FIELDS:
                credits += [dict(c, role="Cover") for c in _people(s.get(f), "Cover")]
        else:
            for f, role in _FIELD_ROLES:
                credits += _people(s.get(f), role)
    credits += _people(d.get("editing"), "Editor")
    seen, deduped = set(), []
    for c in credits:
        k = (c["role"], c["name"].lower())
        if k not in seen:
            seen.add(k)
            deduped.append(c)
    deduped.sort(key=lambda c: _ROLE_ORDER.get(c["role"].lower(), 50))
    desc = "\n\n".join(s["synopsis"].strip() for s in stories
                       if (s.get("type") or "").lower() != "cover" and (s.get("synopsis") or "").strip())
    gid = gcd_issue_id or _id_from_url(d.get("api_url"))
    cover = (d.get("cover") or "").strip()
    price = (d.get("price") or "").split(";")[0].strip() or None
    return {
        "desc": desc,
        "credits": deduped,
        "covers": [{"id": f"g{gid}", "name": "Cover A (Main)", "thumb": cover, "large": cover}] if cover and gid else [],
        "store_date": _date(d.get("on_sale_date")) or _date(d.get("key_date")),
        "cover_date": None,
        "page_count": _int(d.get("page_count")),
        "price": price,
        "isbn": (d.get("isbn") or "").strip() or None,
        "title": (d.get("title") or "").strip() or None,
        "arcs": [],
        "extra": {
            "gcd_id": gid,
            "upc": (d.get("barcode") or "").strip() or None,
            "characters": _dedupe([c for s in stories for c in _split_list(s.get("characters"))]),
            "genre": _dedupe([g.strip() for s in stories for g in re.split(r"[;,]", s.get("genre") or "") if g.strip()]),
            "brand": (d.get("brand_emblem") or "").strip() or None,
            "indicia_publisher": (d.get("indicia_publisher") or "").strip() or None,
            "publication_date": (d.get("publication_date") or "").strip() or None,
        },
        "source": "gcd",
    }


def issue_detail(gcd_issue_id: int) -> dict | None:
    d = _get(f"issue/{int(gcd_issue_id)}/")
    return parse_issue(d, int(gcd_issue_id)) if d else None


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
