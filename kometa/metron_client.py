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
HARVEST_FLOOR = 1000            # ...but the library harvest stops here: the rest is the app's
UNREACHABLE_PAUSE_S = 300       # after a connection failure, everyone fails fast this long
REQUEST_TIMEOUT_S = 15

_lock = threading.Lock()
_state = {"last": 0.0, "paused_until": 0.0, "sustained_remaining": None, "sustained_reset": 0.0}


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
    # An API token (metron.cloud → account → API tokens) is the recommended auth:
    # non-expiring, and donors' higher daily limit is tied to it. Basic auth stays
    # as the fallback for an older config.
    tok = (cfg.get("metron_token") or "").strip()
    if tok:
        return ("token", tok)
    u, p = cfg.get("metron_user"), cfg.get("metron_pass")
    return (u, p) if u and p else None


def harvest_share_spent() -> bool:
    """True once today's remaining Metron allowance is down to HARVEST_FLOOR.
    Unknown (no response seen yet) or past the reset = not spent."""
    left = _state.get("sustained_remaining")
    if left is None:
        return False
    reset = _state.get("sustained_reset") or 0
    if reset and time.time() >= reset:
        _state["sustained_remaining"] = None
        return False
    return left <= HARVEST_FLOOR


def budget() -> dict:
    """Today's Metron allowance as last reported — for the harvest progress view."""
    return {"limit": _state.get("sustained_limit"), "remaining": _state.get("sustained_remaining"),
            "harvest_floor": HARVEST_FLOOR, "harvest_paused": harvest_share_spent()}


def configured() -> bool:
    return _creds() is not None


def test() -> tuple[bool, str]:
    """The Settings dot: ONE light request through the same throttle and the
    same refusal guards as everything else — a test can never be the third strike."""
    if not configured():
        return False, "Not configured"
    try:
        _get("publisher/", page=1)
    except MetronUnavailable as e:
        return False, str(e)
    lim = _state.get("sustained_limit")
    return True, f"Signed in — {lim} requests a day" if lim else "Signed in"


# Metron's fail2ban: 3 refused logins in 5 minutes = a 24-hour IP ban (it
# happened, 2026-10-10). A 401/403 means THESE credentials are bad, so nothing
# asks again with them — not the next trickle, not a restart — until they change
# or AUTH_REFUSED_HOLD_S passes. Stored in config: survives restarts.
AUTH_REFUSED_KEY = "metron_auth_refused"
AUTH_REFUSED_HOLD_S = 6 * 3600
# ...and the per-credential hold alone doesn't stop three DIFFERENT wrong tokens
# (a typo, then another) inside five minutes. After any refusal, no new login is
# tried for this long: at most one refusal per window, so never three in five.
REFUSAL_GAP_S = 600
SERVER_ERROR_PAUSE_S = 300


def _creds_fp(creds) -> str:
    import hashlib
    return hashlib.sha256(repr(creds).encode()).hexdigest()[:16]


def _refused(creds) -> bool:
    from kometa import db
    try:
        mark = db.get_config(db.DB_PATH).get(AUTH_REFUSED_KEY) or ""
    except Exception:
        return False
    fp, _, at = mark.partition("|")
    return fp == _creds_fp(creds) and at and time.time() - float(at) < AUTH_REFUSED_HOLD_S


def _recent_refusal_other(creds) -> int:
    """Seconds still to wait when a DIFFERENT login was refused inside
    REFUSAL_GAP_S, else 0."""
    from kometa import db
    try:
        mark = db.get_config(db.DB_PATH).get(AUTH_REFUSED_KEY) or ""
    except Exception:
        return 0
    fp, _, at = mark.partition("|")
    if not at or fp == _creds_fp(creds):
        return 0
    return max(0, int(float(at) + REFUSAL_GAP_S - time.time()))


def _note_refused(creds, code: int):
    from kometa import db
    try:
        db.set_config({AUTH_REFUSED_KEY: f"{_creds_fp(creds)}|{time.time():.0f}"}, db.DB_PATH)
    except Exception as e:
        logger.warning(f"Metron: couldn't record the refusal: {e}")
    logger.error(f"Metron refused our login ({code}) — not asking again until the credentials change "
                 f"(or {AUTH_REFUSED_HOLD_S // 3600} h): three refusals is a 24-hour ban")


def _get(path: str, **params) -> dict:
    creds = _creds()
    if not creds:
        raise MetronUnavailable("Metron isn't configured")
    if _refused(creds):
        raise MetronUnavailable("Metron refused these credentials — fix them in Settings")
    gap = _recent_refusal_other(creds)
    if gap:
        raise MetronUnavailable(f"Metron refused a login {REFUSAL_GAP_S // 60 - gap // 60} min ago — "
                                f"try again in {gap // 60 + 1} min (three refusals is a 24-hour ban)")
    auth = f"Bearer {creds[1]}" if creds[0] == "token" else "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    from kometa.interactive import is_interactive, is_harvest, MAX_WAIT_S
    interactive = is_interactive()
    if is_harvest() and harvest_share_spent():
        raise MetronUnavailable(f"Harvest's share of today's Metron allowance is spent "
                                f"({HARVEST_FLOOR} kept for the app) — back after the reset")
    if not _lock.acquire(timeout=MAX_WAIT_S if interactive else -1):
        raise MetronUnavailable("Metron is busy with background work — try again in a moment")
    try:                                           # one request at a time, spaced
        now = time.time()
        if now < _state["paused_until"]:
            raise MetronUnavailable("Metron asked us to wait")
        wait = _state["last"] + MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.time()
        req = urllib.request.Request(url, headers={"Authorization": auth, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
                body = json.load(r)
                lim = r.headers.get("X-Ratelimit-Sustained-Limit")
                if lim and _state.get("sustained_limit") != lim:
                    _state["sustained_limit"] = lim
                    logger.info(f"Metron: sustained limit is {lim} a day for this account")
                # Their RATELIMIT.md, verbatim: read BOTH counters on every response and
                # pause until the window resets once a Remaining hits zero — never wait
                # to be told 429. Sustained first: running out for the day costs most.
                left = r.headers.get("X-Ratelimit-Sustained-Remaining")
                if left is not None:
                    _state["sustained_remaining"] = int(left)
                    raw = float(r.headers.get("X-Ratelimit-Sustained-Reset") or 0)
                    # an epoch, or seconds-to-go? Either reads right: nothing real is under 1e9
                    _state["sustained_reset"] = raw if raw > 1e9 else (time.time() + raw if raw else 0.0)
                for scope, floor in (("Sustained", DAILY_FLOOR), ("Burst", 0)):
                    left = r.headers.get(f"X-Ratelimit-{scope}-Remaining")
                    if left is not None and int(left) <= floor:
                        reset = float(r.headers.get(f"X-Ratelimit-{scope}-Reset") or time.time() + 60)
                        _state["paused_until"] = max(_state["paused_until"], reset)
                        logger.warning(f"Metron: {scope.lower()} remaining {left} — pausing until its reset")
                return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = float(e.headers.get("Retry-After") or 60)
                _state["paused_until"] = time.time() + retry
                logger.warning(f"Metron rate-limited us — waiting {int(retry)}s")
            elif e.code in (401, 403):
                _note_refused(creds, e.code)
            elif e.code >= 500:
                _state["paused_until"] = time.time() + SERVER_ERROR_PAUSE_S
                logger.warning(f"Metron answered {e.code} — not asking again for {SERVER_ERROR_PAUSE_S}s")
            raise MetronUnavailable(f"Metron answered {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            # Circuit breaker. With Metron dropping our packets (2026-10-10) every
            # caller sat through its own 30 s timeout, serialised by this lock, and
            # page requests queued behind them until the worker pool ran dry — the
            # Activity page "stalled". One unreachable now fails everyone fast for
            # UNREACHABLE_PAUSE_S; the next call after that probes again.
            _state["paused_until"] = time.time() + UNREACHABLE_PAUSE_S
            logger.warning(f"Metron unreachable ({e}) — not asking again for {UNREACHABLE_PAUSE_S}s")
            raise MetronUnavailable(f"Metron unreachable: {e}") from e
    finally:
        _lock.release()


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
_ROLE_ORDER = {r: i for i, r in enumerate((
    "writer", "story", "script", "plot", "artist", "penciller", "inker", "colorist",
    "letterer", "cover"))}


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
    # the people who MADE the comic first; ten variant-cover artists last
    credits.sort(key=lambda c: (_ROLE_ORDER.get(c["role"].lower(), 50), c["name"] or ""))
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
        "cover_date": d.get("cover_date"),
        "page_count": d.get("page"),
        "price": d.get("price"),
        "isbn": d.get("isbn"),
        "foc_date": d.get("foc_date"),
        "arcs": [a.get("name") for a in d.get("arcs") or [] if isinstance(a, dict)],
        "extra": _extra(d),
        "source": "metron",
    }


def _names(items) -> list[str]:
    return [x.get("name") for x in items or [] if isinstance(x, dict) and x.get("name")]


def _extra(d: dict) -> dict:
    """Everything else Metron says about the issue. Kept since 2026-10-12: the ids
    that join other catalogues (ComicVine, GCD), the barcode, and the cast — what
    a clean ComicInfo.xml is made of (docs/library-enrichment-spec.md, 1d/3a).
    Tolerant by design: a missing field is just absent."""
    series = d.get("series") if isinstance(d.get("series"), dict) else {}
    stype = series.get("series_type")
    out = {
        "cv_id": d.get("cv_id"), "gcd_id": d.get("gcd_id"),
        "upc": d.get("upc") or None, "sku": d.get("sku") or None,
        "title": (d.get("title") or "").strip() or None,
        "story_titles": [n for n in (d.get("name") or []) if n],
        "characters": _names(d.get("characters")), "teams": _names(d.get("teams")),
        "universes": _names(d.get("universes")),
        "reprints": [r.get("issue") for r in d.get("reprints") or [] if isinstance(r, dict) and r.get("issue")],
        "imprint": (d.get("imprint") or {}).get("name") if isinstance(d.get("imprint"), dict) else None,
        "publisher": (d.get("publisher") or {}).get("name") if isinstance(d.get("publisher"), dict) else None,
        "age_rating": (d.get("rating") or {}).get("name") if isinstance(d.get("rating"), dict) else None,
        "series_type": stype.get("name") if isinstance(stype, dict) else stype,
        "series_volume": series.get("volume"), "series_name": (series.get("name") or "").strip() or None,
        "alt_number": d.get("alt_number") or None,
        "cover_hash": d.get("cover_hash") or None, "modified": d.get("modified"),
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def series_detail(series_id: int) -> dict:
    """The run's own identity — what the tidy step names folders and files after.
    {title, year, year_end, publisher, issue_count, type, status}. status is Metron's
    display name — Ongoing / Completed / Cancelled / Hiatus (Ongoing is its default)."""
    d = _get(f"series/{int(series_id)}/")
    pub = d.get("publisher")
    st = d.get("series_type")
    return {
        "title": re.sub(r"\s*\(\d{4}\)\s*$", "", d.get("name") or ""),
        "year": d.get("year_began"),
        "year_end": d.get("year_end"),
        "publisher": pub.get("name") if isinstance(pub, dict) else pub,
        "issue_count": d.get("issue_count"),
        "type": st.get("name") if isinstance(st, dict) else st,
        "status": d.get("status") if isinstance(d.get("status"), str) else None,
    }

