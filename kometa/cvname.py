"""Comic Vine by name (2026-10-17): the credits ladder's Comic Vine rung for an issue no
catalogue handed a Comic Vine id. SQUEE! is why: Metron hasn't the run, GCD has only its
four covers, and Comic Vine has all four issues with Jhonen Vasquez on everything — but
the ladder only asked Comic Vine by an id Metron supplies.

A run is matched to its Comic Vine volume only when the name, the first year (a year
either way: cover dates and on-sale dates straddle New Year) and the publisher all agree,
and exactly one volume does. Then each issue by its number. Both are remembered here, in
their own tables — tracked_series.cv_volume_id belongs to the arcs, which match looser —
and a run with no clean match isn't searched again for MATCH_RETRY_DAYS.

Only for issues with no credits at all: replacing an LOCG or file-tag list by name is the
owner's call, still open. Off until the owner's OK on the dry run (config cv_by_name).
Every Comic Vine call the ladder makes goes through spend():
a second apart, CV_PER_HOUR an hour, so the arcs keep most of Comic Vine's 200 an hour."""
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)

MATCH_RETRY_DAYS = 30        # a run with no clean match: searched again after this
ISSUES_RECHECK_DAYS = 7      # a matched run's issue list: read again for numbers it lacked
CV_PER_HOUR = 120            # the ladder's share of Comic Vine (per resource it's 200 an hour)
CV_GAP_S = 1.0               # Comic Vine flags faster than one a second

_PUB_NOISE = {"comics", "comic", "publishing", "publications", "publication", "inc", "llc", "ltd",
              "entertainment", "press", "books", "studios", "studio", "group", "media", "the", "co"}

_calls: deque = deque()
_lock = threading.Lock()


def spend() -> bool:
    """One Comic Vine call from the ladder: waits out the one-second gap; False when this
    hour's share is gone (the caller couldn't ask — nothing is recorded as tried)."""
    with _lock:
        now = time.monotonic()
        while _calls and now - _calls[0] > 3600:
            _calls.popleft()
        if len(_calls) >= CV_PER_HOUR:
            return False
        if _calls and now - _calls[-1] < CV_GAP_S:
            time.sleep(CV_GAP_S - (now - _calls[-1]))
        _calls.append(time.monotonic())
        return True


def configured(path=None) -> bool:
    """Switched on (config cv_by_name), and Comic Vine set up and on in this DB."""
    cfg = db.get_config(path or db.DB_PATH)
    return (cfg.get("cv_by_name") == "on" and cfg.get("comicvine_enabled", "1") != "0"
            and bool(cfg.get("cv_api_key")))


def has_budget() -> bool:
    """Room left in this hour's share — a read, nothing spent."""
    with _lock:
        now = time.monotonic()
        return sum(1 for t in _calls if now - t <= 3600) < CV_PER_HOUR


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _age_days(at: str | None) -> float:
    if not at:
        return 1e9
    try:
        t = datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or db.DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cv_volume_match (
                tracked_series_id INTEGER PRIMARY KEY,
                cv_volume_id      INTEGER,          -- NULL: searched, no clean match
                why               TEXT,
                at                TEXT,
                issues_at         TEXT              -- the volume's issue list last read
            );
            CREATE TABLE IF NOT EXISTS cv_issue_match (
                tracked_series_id INTEGER,
                number            REAL,
                cv_issue_id       INTEGER,
                PRIMARY KEY (tracked_series_id, number)
            );
        """)


# --- matching ------------------------------------------------------------------------

_YEAR_TAIL = re.compile(r"\s*[(\[]\s*(\d{4})\s*(?:-\s*(?:\d{4})?\s*)?[)\]]\s*$")


def split_year(title: str | None) -> tuple[str, int | None]:
    """'Batman (2025)' → ('Batman', 2025); '(2021-)' too. Our titles carry the run's year to
    tell same-named runs apart; Comic Vine keeps it in start_year, never in the name."""
    m = _YEAR_TAIL.search(title or "")
    return ((title or "")[:m.start()], int(m.group(1))) if m else (title or "", None)


def norm_title(s: str | None) -> str:
    """'The Dark Knight Returns- the Golden Child' and 'Dark Knight Returns: The Golden Child'
    are one name: case, punctuation, dashes, '&' and a leading 'The' don't count."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[-–—:/]", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[4:] if s.startswith("the ") else s


def _pub_words(s: str | None) -> set:
    return {w for w in re.sub(r"[^\w\s]", " ", (s or "").lower()).split() if w not in _PUB_NOISE}


def same_publisher(ours: str | None, theirs: str | None) -> bool:
    """'Image Comics' is 'Image'; 'BOOM! Studios' is 'Boom! Studios'. Unknown on our side passes."""
    a, b = _pub_words(ours), _pub_words(theirs)
    if not a:
        return True
    return bool(b) and (a <= b or b <= a)


def series_year(series: dict, path=None) -> int | None:
    """The run's first year: its own, the one in its title, else its earliest dated issue."""
    if series.get("year_began"):
        return int(series["year_began"])
    if split_year(series.get("title"))[1]:
        return split_year(series["title"])[1]
    with db._connect(path or db.DB_PATH) as conn:
        r = conn.execute("SELECT MIN(store_date) d FROM issue_status WHERE tracked_series_id = ? "
                         "AND store_date IS NOT NULL AND store_date < '9000'", (series["id"],)).fetchone()
    return int(r["d"][:4]) if r and r["d"] else None


def pick_volume(series: dict, year: int | None, volumes: list[dict]) -> tuple[int | None, str]:
    """(volume id, why) — the one volume whose name, year and publisher all agree, or None."""
    name = norm_title(split_year(series.get("title"))[0])
    fits = [v for v in volumes
            if norm_title(v.get("name")) == name
            and same_publisher(series.get("publisher"), v.get("publisher"))
            and (year is None or (v.get("year") is not None and abs(int(v["year"]) - year) <= 1))]
    if len(fits) == 1:
        return int(fits[0]["cv_volume_id"]), f"{fits[0].get('name')} ({fits[0].get('year')}, {fits[0].get('publisher')})"
    if not fits:
        return None, "no volume with this name, year and publisher"
    if year is None:
        return None, f"{len(fits)} volumes fit and the run has no year"
    # two or more: the exact year breaks the tie, if it's one volume
    exact = [v for v in fits if v.get("year") is not None and int(v["year"]) == year]
    if len(exact) == 1:
        return int(exact[0]["cv_volume_id"]), f"{exact[0].get('name')} ({exact[0].get('year')}, {exact[0].get('publisher')})"
    return None, f"{len(fits)} volumes fit"


# --- what's known, without asking ------------------------------------------------------

def known(path=None) -> tuple[dict, dict]:
    """(volumes by series id, issue ids by (series id, number)) — one read for a whole tick."""
    ensure_tables(path)
    with db._connect(path or db.DB_PATH) as conn:
        vols = {r["tracked_series_id"]: dict(r) for r in conn.execute("SELECT * FROM cv_volume_match")}
        issues = {(r["tracked_series_id"], r["number"]): r["cv_issue_id"] for r in conn.execute("SELECT * FROM cv_issue_match")}
    return vols, issues


def could_ask(series_id: int, number: float, state: tuple[dict, dict]) -> bool:
    """Is there anything to do by name for this issue — a search not yet run (or due again),
    or a matched run that has this number or whose issue list is due a re-read?"""
    vols, issues = state
    v = vols.get(series_id)
    if v is None:
        return True
    if not v["cv_volume_id"]:
        return _age_days(v["at"]) >= MATCH_RETRY_DAYS
    return bool(issues.get((series_id, number))) or _age_days(v["issues_at"]) >= ISSUES_RECHECK_DAYS


# --- asking ----------------------------------------------------------------------------

def _match_volume(cv, series: dict, path) -> int | None:
    year = series_year(series, path)
    query = re.sub(r"\s+", " ", re.sub(r"[-–—:/]", " ", split_year(series["title"])[0])).strip()
    if not spend():
        return None
    volumes = cv.search_volumes(query, limit=20)
    vid, why = pick_volume(series, year, volumes)
    with db._connect(path) as conn:
        conn.execute("INSERT INTO cv_volume_match (tracked_series_id, cv_volume_id, why, at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(tracked_series_id) DO UPDATE SET cv_volume_id = excluded.cv_volume_id, "
                     "why = excluded.why, at = excluded.at", (series["id"], vid, why, _now()))
    logger.info(f"Comic Vine by name: {series['title']} → {why}")
    return vid


def _read_issues(cv, series_id: int, vid: int, path) -> bool:
    """The volume's issues, every page, numbers → ids. False if a page couldn't be read."""
    found, offset = {}, 0
    while True:
        if not spend():
            return False
        d = cv._get("issues/", filter=f"volume:{vid}", field_list="id,issue_number", limit=100, offset=offset)
        page = d.get("results") or []
        for i in page:
            try:
                found[float(i.get("issue_number"))] = int(i["id"])
            except (TypeError, ValueError):
                continue                          # '½', 'A': not a number we track
        offset += len(page)
        if not page or offset >= int(d.get("number_of_total_results") or 0):
            break
    with db._connect(path) as conn:
        conn.executemany("INSERT OR REPLACE INTO cv_issue_match (tracked_series_id, number, cv_issue_id) VALUES (?, ?, ?)",
                         [(series_id, n, i) for n, i in found.items()])
        conn.execute("UPDATE cv_volume_match SET issues_at = ? WHERE tracked_series_id = ?", (_now(), series_id))
    return True


def issue_id(series_id: int, number: float, path=None) -> int | None:
    """This issue's Comic Vine id, found by its run's name: matched (or re-checked) on demand,
    remembered either way. None when there's no clean match, no such number, or no budget."""
    path = path or db.DB_PATH
    state = known(path)
    if not could_ask(series_id, number, state):
        return state[1].get((series_id, number))
    from kometa import sources
    cv = sources.comicvine()
    if cv is None:
        return None
    vols, issues = state
    v = vols.get(series_id)
    vid = v["cv_volume_id"] if v else None
    try:
        if not vid:
            series = db.get_series_by_id(series_id, path)
            if not series or series.get("kind") == "arc":
                return None
            vid = _match_volume(cv, series, path)
            if not vid:
                return None
        if (series_id, number) not in issues or _age_days((v or {}).get("issues_at")) >= ISSUES_RECHECK_DAYS:
            if not _read_issues(cv, series_id, vid, path):
                return None
    except Exception as e:
        logger.info(f"Comic Vine by name skipped {series_id}#{number:g}: {e}")
        return None
    return known(path)[1].get((series_id, number))


def summary(path=None) -> dict:
    """Counts for the dry run: runs matched, not matched, issues mapped."""
    vols, issues = known(path)
    return {"runs_matched": sum(1 for v in vols.values() if v["cv_volume_id"]),
            "runs_unmatched": sum(1 for v in vols.values() if not v["cv_volume_id"]),
            "issues_mapped": sum(1 for i in issues.values() if i)}
