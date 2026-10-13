"""Is each outside source actually WORKING? (build item x10)

The Settings badge already lights for setup you can fix (no login, no pass). It
couldn't see a source that's set up fine and failing anyway — 2026-10-13 Fandom's
image CDN 403'd every request for hours, the setting on, nothing misconfigured,
not a dot anywhere. So every request to an outside source reports here:

  record(source, 'ok' | 'refused' | 'error', detail)

'ok' is any answer (a 404 is an answer); 'refused' is the host saying no (403 /
429 / a challenge); 'error' is an outage, a timeout, a 5xx. Kept in memory per
source, written to source_health when the state changes or every few minutes —
not a DB write per image.

A source is unwell (main.settings_attention lights its card) when, since its last
success, it has failed UNWELL_STREAK times in a row and the first of those is at
least UNWELL_AFTER_S old — one bad minute isn't news, an evening of it is — or when
nothing has worked in a day while it kept trying.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)

UNWELL_STREAK = 5
UNWELL_AFTER_S = 30 * 60
DAY_S = 24 * 3600
FLUSH_EVERY_S = 300
PERSIST = True               # tests outside test_health switch the DB writes off (conftest)

# source name (as each client reports it) → the Settings card its dot belongs on
CARDS = {
    "Metron": "metron", "LOCG": "locg", "GCD": "gcd", "ComicVine": "comicvine",
    "Fandom wikis": "fandom", "Hardcover": "hardcover", "PRH": "prh", "Open Library": "openlibrary",
    "ComicBookRoundup": "cbr", "ComicBookRoundup people": "cbr",
    "Marvel.com": "marveldc", "Marvel CDN": "marveldc", "DC.com": "marveldc",
    # the image lanes, on the card of whoever runs the host
    "Images static.wikia.nocookie.net": "fandom", "Images comicvine.gamespot.com": "comicvine",
    "Images static.metron.cloud": "metron", "Images s3.amazonaws.com": "locg",
    "Images files1.comics.org": "gcd",
}

_lock = threading.Lock()
_mem: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def ensure_table(path=None):
    with db._connect(path or db.DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY, last_ok REAL, last_bad REAL, first_bad REAL,
            streak INTEGER DEFAULT 0, kind TEXT, detail TEXT, written REAL)""")


def _load(path) -> None:
    if _mem or not PERSIST:
        return
    try:
        ensure_table(path)
        with db._connect(path) as conn:
            for r in conn.execute("SELECT source, last_ok, last_bad, first_bad, streak, kind, detail FROM source_health"):
                _mem[r[0]] = {"last_ok": r[1], "last_bad": r[2], "first_bad": r[3], "streak": r[4] or 0,
                              "kind": r[5], "detail": r[6], "written": _now()}
    except Exception as e:
        logger.info(f"Source health: load skipped: {e}")


def record(source: str, outcome: str, detail: str | None = None, path=None) -> None:
    """Never raises: bookkeeping must not break the request it's watching."""
    try:
        path = path or db.DB_PATH
        now = _now()
        with _lock:
            _load(path)
            s = _mem.setdefault(source, {"last_ok": None, "last_bad": None, "first_bad": None, "streak": 0,
                                         "kind": None, "detail": None, "written": 0.0})
            was_bad = s["streak"] > 0
            if outcome == "ok":
                s.update(last_ok=now, streak=0, first_bad=None)
                changed = was_bad
            else:
                if not was_bad:
                    s["first_bad"] = now
                s.update(last_bad=now, streak=s["streak"] + 1, kind=outcome, detail=(detail or "")[:200])
                changed = not was_bad
            if not PERSIST or not (changed or now - s["written"] >= FLUSH_EVERY_S):
                return
            s["written"] = now
            row = (source, s["last_ok"], s["last_bad"], s["first_bad"], s["streak"], s["kind"], s["detail"], now)
        ensure_table(path)
        with db._connect(path) as conn:
            conn.execute("""INSERT OR REPLACE INTO source_health
                (source, last_ok, last_bad, first_bad, streak, kind, detail, written) VALUES (?,?,?,?,?,?,?,?)""", row)
    except Exception as e:
        logger.info(f"Source health: {source} not recorded: {e}")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%-d %b %H:%M")


def unwell(path=None, now: float | None = None) -> list[dict]:
    """[{card, source, reason}] for every source failing past the thresholds."""
    path = path or db.DB_PATH
    now = now or _now()
    with _lock:
        _load(path)
        snap = {k: dict(v) for k, v in _mem.items()}
    out = []
    for source, s in snap.items():
        if not s["streak"] or not s["first_bad"]:
            continue
        failing_long = s["streak"] >= UNWELL_STREAK and now - s["first_bad"] >= UNWELL_AFTER_S
        dry_day = (s["last_ok"] is None or now - s["last_ok"] >= DAY_S) and now - s["first_bad"] >= DAY_S
        if not (failing_long or dry_day):
            continue
        what = "refusing us" if s["kind"] == "refused" else "failing"
        reason = f"{source}: {what} since {_when(s['first_bad'])} ({s['streak']} in a row"
        reason += f", last: {s['detail']})" if s["detail"] else ")"
        out.append({"card": CARDS.get(source, "harvest"), "source": source, "reason": reason})
    return out


def reset():
    """Tests only: forget everything in memory."""
    with _lock:
        _mem.clear()


def snapshot(path=None) -> dict:
    """Every source's state, for the progress page / debugging."""
    with _lock:
        _load(path or db.DB_PATH)
        return {k: {kk: v[kk] for kk in ("last_ok", "last_bad", "first_bad", "streak", "kind", "detail")} for k, v in _mem.items()}
