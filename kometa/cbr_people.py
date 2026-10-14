"""ComicBookRoundup's PEOPLE — "readers like you" (enrichment 1i-g, approved 2026-10-13).

cbr.py reads a run's series page: one average per issue. This module reads the
people behind the averages. Every critic and every reader who scored an issue
has a page listing everything else they ever scored, so a person who loved the
same runs you did is a stranger with your taste, and what THEY loved that you
don't own is a recommendation with receipts.

Shapes (6 live GETs, 2026-10-13, all 200, no Cloudflare, no pagination anywhere):
- Issue page /comic-books/reviews/{pub}/{series}/{n}: critics under
  div#reviews-critic (<h3>{Outlet} - <a href="/comic-books/reviewer/{slug}">),
  readers under ul#reviews (div.review.user score, /user/profile/{id}). A reader
  who only ticked 'read' has an icon and no score: skipped.
- Critic page /comic-books/reviewer/{slug} and reader page /user/reviews/{id}:
  one div.issues-table, each score in a td.ratingCell followed by its issue
  link and a date. Runs with several issues get a summary row (the run's link,
  no number) and the issues nested under it; the summary is skipped.
  (/user/profile/{id} is only recent activity — never asked.)
Names arrive with mojibake entities, so a person is keyed on 'critic:{slug}' /
'user:{id}', never on the name.

The harvest, slowest worker in the building:
- Pass A, issue pages: for every run cbr.py linked, #1 plus the three
  most-reviewed issues we have CBR counts for (community_json['cbr']). Runs you
  read/starred/rated go first, so the discover row is useful before the pass ends.
- Pass B, profiles: only people who scored >= 2 of our linked runs, most
  overlap first. Then profiles older than PROFILE_DAYS are asked again.
  An issue page is asked once; a run linked later brings its pages with it.
- ONE page per tick, a tick a minute, its OWN limiter (60 s gap, 480 a day —
  NOT cbr.limiter's 500, which the series relinks need), and only inside
  'cbr_people_hours' (default 23-7, Brisbane, wraps midnight — marveldc's
  window code). Off with 'cbr_people_enabled' = '0' or 'cbr_enabled' = '0'.
- A page that doesn't read as expected is marked and skipped; three in a row
  and the whole worker rests six hours (better idle than mis-recorded).

GET /api/discover/community ranks runs you don't own by how your taste-twins
scored them (see `community`). GET /api/harvest/cbr-people-report says how far
the harvest has got.
"""
import html
import logging
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

import kometa.db as db
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH

SITE = "https://comicbookroundup.com"
REVIEWS = "/comic-books/reviews/"
DEFAULT_HOURS = "0-0"                      # all day: a website, not a fish and chip shop (start == end)
TOP_ISSUES = 3                # most-reviewed issues per run, beside #1
MIN_OVERLAP = 2               # linked runs a person must have scored before their profile is worth a request
PROFILE_DAYS = 30
MAX_TRIES = 2                 # a page that won't read gets one more go, then it's left alone
SHAPE_STREAK = 3              # this many unreadable pages in a row = the site changed, not the page
SHAPE_REST_S = 6 * 3600

limiter = Limiter("ComicBookRoundup people", min_interval_s=60.0, daily_cap=1440,
                  refused_pause_s=3 * 3600, timeout_s=30)


def _stop_aware_sleep(seconds: float):
    from kometa.activity import STOPPING
    STOPPING.wait(seconds)


limiter.sleep = _stop_aware_sleep         # a deploy doesn't sit out a 60 s gap


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS cbr_people (
            person TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, name TEXT, outlet TEXT,
            first_seen TEXT, fetched_at TEXT, status TEXT, tries INTEGER NOT NULL DEFAULT 0, scores INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS cbr_people_scores (
            person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL, score REAL NOT NULL,
            date TEXT, via TEXT, at TEXT, PRIMARY KEY (person, series_path, number))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cbr_people_scores_series ON cbr_people_scores(series_path)")
        conn.execute("""CREATE TABLE IF NOT EXISTS cbr_people_pages (
            page_path TEXT PRIMARY KEY, series_path TEXT, number TEXT, fetched_at TEXT, status TEXT,
            tries INTEGER NOT NULL DEFAULT 0, scores INTEGER)""")
        conn.execute("CREATE TABLE IF NOT EXISTS cbr_people_series (series_path TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS cbr_people_state (key TEXT PRIMARY KEY, value TEXT)")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_local() -> datetime:
    from kometa.scheduler import TZ
    return datetime.now(TZ)


def _hours(path) -> tuple[int, int]:
    from kometa.marveldc import parse_hours
    return parse_hours(db.get_config(path).get("cbr_people_hours") or DEFAULT_HOURS)


def _get_state(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM cbr_people_state WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set_state(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO cbr_people_state (key, value) VALUES (?, ?)", (key, str(value)))


# --- parsing ----------------------------------------------------------------------------
_SCORE_RE = re.compile(r'class="review[^"]*"\s*>\s*<span>([^<]*)</span>')
_USER_SCORE_RE = re.compile(r'class="review user[^"]*"\s*>\s*<span>([^<]*)</span>')
_ISSUE_LINK_RE = re.compile(r'href="(/comic-books/reviews/[^"/?#\s]+/[^"/?#\s]+)/([^"/?#\s]+)"[^>]*>(.*?)</a>', re.S)
_DATE_RE = re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")


def _text(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _score(s: str | None) -> float | None:
    try:
        v = float((s or "").strip())
    except ValueError:
        return None                           # 'N/A': scored nothing
    return v if 0 <= v <= 10 else None


def _date(s: str | None) -> str | None:
    s = re.sub(r"\s+", " ", (s or "").replace(".", "")).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s or None


def series_path_of(url: str | None) -> str | None:
    """'https://comicbookroundup.com/comic-books/reviews/image-comics/saga/' → '/comic-books/reviews/image-comics/saga'."""
    m = re.search(r"(/comic-books/reviews/[^/?#\s]+/[^/?#\s]+)", url or "")
    return m.group(1) if m else None


def parse_issue_page(page: str) -> dict | None:
    """{critics: [{ref, name, outlet, score, date}], users: [{ref, name, score, date}]}.
    None when it doesn't look like an issue page at all."""
    page = page or ""
    c0, u0 = page.find('id="reviews-critic"'), page.find('id="reviews"')
    if c0 < 0 and u0 < 0:
        return None
    critics, users = [], []
    if c0 >= 0:
        cend = page.find('id="reviews-user"', c0)
        for li in re.split(r"<li\b", page[c0:cend if cend > c0 else (u0 if u0 > c0 else len(page))])[1:]:
            m = _SCORE_RE.search(li)
            who = re.search(r'<h3>(.*?)<a[^>]*href="/comic-books/reviewer/([^"/?#\s]+)"[^>]*>(.*?)</a>', li, re.S)
            if not m or not who or _score(m.group(1)) is None:
                continue
            d = re.search(r'<span class="date">([^<]*)</span>', li)
            critics.append({"ref": who.group(2), "name": _text(who.group(3)) or None,
                            "outlet": _text(who.group(1)).rstrip(" -").strip() or None,
                            "score": _score(m.group(1)), "date": _date(d.group(1)) if d else None})
    if u0 >= 0:
        for chunk in page[u0:].split('class="list-review"')[1:]:
            det = chunk.find("user-review-details")
            if det < 0:
                continue
            m = _USER_SCORE_RE.search(chunk[:det])          # a 'read it' tick has an icon here, no score
            who = re.search(r'<h3>\s*<a[^>]*href="/user/profile/(\d+)"[^>]*>(.*?)</a>', chunk[det:], re.S)
            if not m or not who or _score(m.group(1)) is None:
                continue
            d = re.search(r'<span class="date">([^<]*)</span>', chunk[det:])
            users.append({"ref": who.group(1), "name": _text(who.group(2)) or None,
                          "score": _score(m.group(1)), "date": _date(d.group(1)) if d else None})
    return {"critics": critics, "users": users}


def parse_profile_page(page: str) -> dict | None:
    """A critic's /comic-books/reviewer/{slug} or a reader's /user/reviews/{id}:
    {name, outlet, scores: [{series_path, number, score, date, series_name}]}.
    None without the issues table — the shape we know is gone."""
    page = page or ""
    t0 = page.find('class="issues-table"')
    if t0 < 0:
        return None
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = _text(h1.group(1)) if h1 else ""
    name = re.sub(r"(?:\s+Comic Reviews|'s Profile)$", "", name).strip() or None
    out = re.search(r"Reviewer For:\s*</td>\s*<td>(.*?)</td>", page, re.S)
    outlet = ", ".join(_text(x) for x in re.findall(r"<span>(.*?)</span>", out.group(1), re.S)) if out else None
    scores = []
    for chunk in page[t0:].split('class="ratingCell"')[1:]:
        m = _SCORE_RE.search(chunk)                     # first score in the cell: the critic's on a critic
        if not m or _score(m.group(1)) is None:         # page, the reader's on a reader page
            continue
        link = _ISSUE_LINK_RE.search(chunk, m.end())
        if not link:
            continue                                    # a run's summary row: its link has no issue number
        d = _DATE_RE.search(chunk, link.end())
        scores.append({"series_path": link.group(1), "number": link.group(2), "score": _score(m.group(1)),
                       "date": _date(d.group(1)) if d else None,
                       "series_name": re.sub(r"\s+#\S*$", "", _text(link.group(3))) or None})
    return {"name": name, "outlet": outlet or None, "scores": scores}


def profile_url(person: str) -> str:
    kind, ref = person.split(":", 1)
    return f"{SITE}/comic-books/reviewer/{ref}" if kind == "critic" else f"{SITE}/user/reviews/{ref}"


# --- the queue --------------------------------------------------------------------------
def _linked(conn) -> dict[str, int]:
    """{series_path: tracked_series_id} for every run cbr.py linked."""
    out = {}
    for r in conn.execute("SELECT tracked_series_id, url FROM cbr_series WHERE state = 'linked' AND url IS NOT NULL"):
        p = series_path_of(r["url"])
        if p:
            out[p] = r["tracked_series_id"]
    return out


def issue_pages(path=None) -> list[tuple[str, str, str]]:
    """Pass A's whole list, (page_path, series_path, number): every linked run's #1
    plus its TOP_ISSUES most-reviewed issues. Your seed runs first."""
    path = path or DB_PATH
    _ready(path)
    seeds = seed_weights(path)
    with db._connect(path) as conn:
        linked = _linked(conn)
        counts: dict[str, list[tuple[int, str]]] = {}
        for r in conn.execute("""SELECT r.community_json FROM issue_record r
                JOIN cbr_series c ON c.tracked_series_id = r.tracked_series_id AND c.state = 'linked'
                WHERE r.community_json LIKE '%"cbr": {%'"""):
            try:
                snap = (json.loads(r["community_json"] or "{}") or {}).get("cbr") or {}
            except (TypeError, ValueError):
                continue
            n = int(snap.get("critic_reviews") or 0) + int(snap.get("user_reviews") or 0)
            m = re.search(r"(/comic-books/reviews/[^/?#\s]+/[^/?#\s]+)/([^/?#\s]+)$", snap.get("url") or "")
            if n > 0 and m:
                counts.setdefault(m.group(1), []).append((n, m.group(2)))
    out = []
    for sp in sorted(linked, key=lambda p: (-seeds.get(linked[p], 0.0), linked[p])):
        top = [num for _, num in sorted(counts.get(sp, []), key=lambda t: -t[0]) if num != "1"][:TOP_ISSUES]
        out += [(f"{sp}/{num}", sp, num) for num in ["1"] + top]
    return out


def _overlaps(conn, linked: dict[str, int]) -> dict[str, int]:
    """{person: how many of our linked runs they scored}."""
    out: dict[str, int] = {}
    for r in conn.execute("SELECT DISTINCT person, series_path FROM cbr_people_scores"):
        if r["series_path"] in linked:
            out[r["person"]] = out.get(r["person"], 0) + 1
    return out


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=PROFILE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def next_job(path=None) -> tuple | None:
    """('issue', page_path, series_path, number) | ('profile', person) | None.
    Pass A first, then never-read profiles, then stale ones; most overlap first."""
    path = path or DB_PATH
    _ready(path)
    with db._connect(path) as conn:
        settled = {r[0] for r in conn.execute(
            "SELECT page_path FROM cbr_people_pages WHERE status IN ('done', 'gone', 'unreadable')")}
    for page_path, sp, num in issue_pages(path):
        if page_path not in settled:
            return ("issue", page_path, sp, num)
    stale = _stale_cutoff()
    with db._connect(path) as conn:
        over = _overlaps(conn, _linked(conn))
        people = {r["person"]: dict(r) for r in conn.execute("SELECT person, fetched_at, status FROM cbr_people")}
    keen = sorted((p for p, n in over.items() if n >= MIN_OVERLAP and p in people
                   and people[p]["status"] != "unreadable"), key=lambda p: (-over[p], p))
    for p in keen:
        if not people[p]["fetched_at"]:
            return ("profile", p)
    for p in keen:
        if people[p]["fetched_at"] < stale:
            return ("profile", p)
    return None


# --- recording --------------------------------------------------------------------------
def _upsert_person(conn, person: str, name: str | None, outlet: str | None = None):
    kind, ref = person.split(":", 1)
    conn.execute("""INSERT INTO cbr_people (person, kind, ref, name, outlet, first_seen) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(person) DO UPDATE SET name = COALESCE(excluded.name, name), outlet = COALESCE(excluded.outlet, outlet)""",
                 (person, kind, ref, name, outlet, _stamp()))


def _put_score(conn, person: str, sp: str, num: str, score: float, date: str | None, via: str):
    conn.execute("""INSERT OR REPLACE INTO cbr_people_scores (person, series_path, number, score, date, via, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""", (person, sp, str(num), score, date, via, _stamp()))


def _mark_page(conn, page_path, sp, num, status, n=None):
    conn.execute("""INSERT INTO cbr_people_pages (page_path, series_path, number, fetched_at, status, tries, scores)
        VALUES (?, ?, ?, ?, ?, 0, ?) ON CONFLICT(page_path) DO UPDATE SET fetched_at = excluded.fetched_at,
        status = excluded.status, scores = excluded.scores""", (page_path, sp, num, _stamp(), status, n))


def record_issue_page(page_path: str, sp: str, num: str, parsed: dict, path=None) -> int:
    with db._connect(path or DB_PATH) as conn:
        for kind in ("critics", "users"):
            for c in parsed[kind]:
                who = f"{kind[:-1]}:{c['ref']}"
                _upsert_person(conn, who, c["name"], c.get("outlet"))
                _put_score(conn, who, sp, num, c["score"], c["date"], "issue")
        n = len(parsed["critics"]) + len(parsed["users"])
        _mark_page(conn, page_path, sp, num, "done", n)
    return n


def record_profile(person: str, parsed: dict, path=None) -> int:
    with db._connect(path or DB_PATH) as conn:
        _upsert_person(conn, person, parsed["name"], parsed["outlet"])
        for s in parsed["scores"]:
            _put_score(conn, person, s["series_path"], s["number"], s["score"], s["date"], "profile")
            if s["series_name"] and re.fullmatch(r"\d+(?:\.\d+)?", s["number"]):   # 'Run #12' names the run;
                # an annual or special's link text ('Adventure Time: 2013 SPOooktacular') doesn't
                conn.execute("INSERT OR REPLACE INTO cbr_people_series (series_path, name) VALUES (?, ?)",
                             (s["series_path"], s["series_name"]))
        conn.execute("UPDATE cbr_people SET fetched_at = ?, status = 'done', tries = 0, scores = ? WHERE person = ?",
                     (_stamp(), len(parsed["scores"]), person))
    return len(parsed["scores"])


def _failed(conn, job: tuple, why: str, shape: bool):
    """A page that answered but didn't read (or answered oddly): one more go,
    then left alone. SHAPE_STREAK unreadable pages in a row rests the worker."""
    if job[0] == "issue":
        _, page_path, sp, num = job
        conn.execute("""INSERT INTO cbr_people_pages (page_path, series_path, number, status, tries)
            VALUES (?, ?, ?, 'error', 1) ON CONFLICT(page_path) DO UPDATE SET tries = tries + 1""", (page_path, sp, num))
        tries = conn.execute("SELECT tries FROM cbr_people_pages WHERE page_path = ?", (page_path,)).fetchone()[0]
        if tries >= MAX_TRIES:
            conn.execute("UPDATE cbr_people_pages SET status = 'unreadable', fetched_at = ? WHERE page_path = ?",
                         (_stamp(), page_path))
    else:
        conn.execute("UPDATE cbr_people SET tries = tries + 1 WHERE person = ?", (job[1],))
        tries = conn.execute("SELECT tries FROM cbr_people WHERE person = ?", (job[1],)).fetchone()[0]
        if tries >= MAX_TRIES:
            conn.execute("UPDATE cbr_people SET status = 'unreadable', fetched_at = ? WHERE person = ?",
                         (_stamp(), job[1]))
    streak = int(_get_state(conn, "shape_streak") or 0) + 1 if shape else 0
    _set_state(conn, "last_error", f"{_stamp()} {why}")
    if streak >= SHAPE_STREAK:
        streak = 0
        _set_state(conn, "last_error", f"{_stamp()} {SHAPE_STREAK} pages in a row didn't read — resting "
                                       f"{SHAPE_REST_S // 3600} h (last: {why})")
        limiter.rest(SHAPE_REST_S)
    _set_state(conn, "shape_streak", streak)


# --- the tick ---------------------------------------------------------------------------
def _ready(path):
    from kometa import cbr, marks, record
    cbr.ensure_tables(path)
    record.ensure_tables(path)
    marks.ensure_tables(path)
    ensure_tables(path)


POISON_TRIES = 3


def tick(path=None, now: datetime | None = None, get=None) -> int:
    """Scheduler tick (every minute): outside the night window, switched off,
    resting or spent — nothing, not even a query. Otherwise ONE page.
    Returns scores recorded."""
    from kometa.marveldc import in_window
    path = path or DB_PATH
    if not enabled("cbr_enabled", path) or not enabled("cbr_people_enabled", path):
        return 0
    now = now or _now_local()
    if not in_window(now.hour, *_hours(path)) or limiter.spent():
        return 0
    job = next_job(path)
    if not job:
        return 0
    url = SITE + job[1] if job[0] == "issue" else profile_url(job[1])
    try:
        r = (get or limiter.get)(url)
    except SourceUnavailable as e:
        with db._connect(path) as conn:
            _set_state(conn, "last_error", f"{_stamp()} {e}")
            # One page that always 500s (user:4644, 2026-10-14) stopped every tick from
            # 05:09 on — nothing marked, so the next tick asked for it again. The same
            # page failing POISON_TRIES times in a row is unreadable; the harvest moves on.
            key = f"{job[0]}:{job[1]}"
            if " answered 5" in str(e):
                n = int(_get_state(conn, "fail_count") or 0) + 1 if _get_state(conn, "fail_job") == key else 1
                _set_state(conn, "fail_job", key)
                _set_state(conn, "fail_count", n)
                if n >= POISON_TRIES:
                    if job[0] == "issue":
                        _mark_page(conn, job[1], job[2], job[3], "unreadable", 0)
                    else:
                        conn.execute("UPDATE cbr_people SET fetched_at = ?, status = 'unreadable' WHERE person = ?", (_stamp(), job[1]))
                    _set_state(conn, "fail_count", 0)
                    logger.info(f"CBR people: {key} failed {n} times in a row — set aside as unreadable")
        logger.info(f"CBR people stopped this tick: {e}")
        return 0
    if r.status_code == 404:
        with db._connect(path) as conn:
            if job[0] == "issue":
                _mark_page(conn, job[1], job[2], job[3], "gone", 0)
            else:
                conn.execute("UPDATE cbr_people SET fetched_at = ?, status = 'gone' WHERE person = ?", (_stamp(), job[1]))
        return 0
    parsed = None
    if r.status_code == 200:
        parsed = parse_issue_page(r.text) if job[0] == "issue" else parse_profile_page(r.text)
    if parsed is None:
        with db._connect(path) as conn:
            _failed(conn, job, f"{url} answered {r.status_code}" if r.status_code != 200
                    else f"{url} didn't read as a {job[0]} page", shape=r.status_code == 200)
        return 0
    n = record_issue_page(*job[1:], parsed, path) if job[0] == "issue" else record_profile(job[1], parsed, path)
    with db._connect(path) as conn:
        _set_state(conn, "shape_streak", 0)
    b = limiter.budget()
    logger.info(f"CBR people: {job[0]} {job[1]} → {n} scores (today {b['used']}/{b['cap']})")
    return n


# --- what it did ------------------------------------------------------------------------
def report(path=None) -> dict:
    """Pages done/left per pass, people, scores, today's requests, the last error."""
    path = path or DB_PATH
    _ready(path)
    pages = issue_pages(path)
    stale = _stale_cutoff()
    with db._connect(path) as conn:
        st = {r[0]: r[1] for r in conn.execute("SELECT page_path, status FROM cbr_people_pages")}
        over = _overlaps(conn, _linked(conn))
        people = {r["person"]: dict(r) for r in conn.execute("SELECT person, kind, fetched_at, status FROM cbr_people")}
        n_scores = conn.execute("SELECT COUNT(*) FROM cbr_people_scores").fetchone()[0]
        last_error = _get_state(conn, "last_error")
    a = [st.get(p) for p, _, _ in pages]
    a_done = sum(1 for s in a if s in ("done", "gone", "unreadable"))
    keen = [p for p, n in over.items() if n >= MIN_OVERLAP and p in people]
    b_done = sum(1 for p in keen if people[p]["status"] == "unreadable"
                 or (people[p]["fetched_at"] and people[p]["fetched_at"] >= stale))
    start, end = _hours(path)
    return {"pass_a_issue_pages": {"done": a_done, "left": len(pages) - a_done,
                                   "gone": a.count("gone"), "unreadable": a.count("unreadable")},
            "pass_b_profiles": {"done": b_done, "left": len(keen) - b_done},
            "people": {"critics": sum(1 for p in people.values() if p["kind"] == "critic"),
                       "users": sum(1 for p in people.values() if p["kind"] == "user"),
                       "profiles_read": sum(1 for p in people.values() if p["status"] == "done")},
            "scores": n_scores, "requests_today": limiter.budget(), "last_error": last_error,
            "hours": f"{start}-{end}",
            "enabled": enabled("cbr_enabled", path) and enabled("cbr_people_enabled", path)}


@router.get("/api/harvest/cbr-people-report")
def get_report():
    return report()


# --- readers like you -------------------------------------------------------------------
# The maths, one person p at a time:
#   centred(p, s) = p's mean score on run s − p's mean over EVERY score they gave.
#     The reader who gives everything a 9 centres to ~0 everywhere and so weighs
#     nothing; the hard marker's 8 counts as the rave it is.
#   w_p = Σ_{seed s p scored} seed_weight(s) · centred(p, s) · n/(n+SHRINK_K)   (n = seeds p scored)
#     kept only with n ≥ MIN_SEEDS and w_p > 0: agreeing with you on one run is luck.
#     A run you rated low has a NEGATIVE seed weight: loving what you panned counts against.
#   Critics and readers are separate pools, each normalised to sum 1, blended BLEND.
#   score(c) = Σ_p w_p · centred(p, c) / (Σ_{p who scored c} |w_p| + LAMBDA)
#     LAMBDA shrinks a run two twins adore below one twenty of them like.
#   ≥ MIN_SUPPORTERS distinct people with centred(p, c) > 0, or it isn't shown.
# The per-run rows ("readers who liked X also like…") are the same machinery with
# one seed: everyone who scored X above their own mean, n = 1 allowed.
BLEND = {"critic": 0.5, "user": 0.5}
SHRINK_K = 5
LAMBDA = 1.0
MIN_SEEDS = 2
MIN_SUPPORTERS = 3
CACHE_TTL_S = 3600

# What a run says about you (the user, 2026-10-13): what you're reading now loudest,
# then your own ratings, favourites, what you've finished, and the rest of the shelf
# quietly. A run takes its strongest tier, plus its rating term.
W_ONDECK = 3.0                # on On Deck right now: Continue reading or Next up
W_FAVOURITE = 1.5             # starred (the run, or an issue of it)
W_READ = 1.0                  # finished an issue of it
W_OWNED = 0.5                 # on the shelf, nothing more known
W_RATING = 1.0                # per star away from 3: 5★ +2, 4★ +1, 3★ 0, 2★ −1, 1★ −2 (own rating, else its issues' mean)
NEUTRAL_RATING = 3

# display names for the publisher slugs cbr.py writes (its _PUBLISHERS, reversed)
_PUBLISHER_NAMES = {"marvel-comics": "Marvel", "dc-comics": "DC", "vertigo": "Vertigo", "image-comics": "Image",
                    "boom-studios": "BOOM! Studios", "dark-horse-comics": "Dark Horse", "idw-publishing": "IDW",
                    "dynamite": "Dynamite", "mad-cave-studios": "Mad Cave"}

_cache: dict = {}
_cache_lock = threading.Lock()


def currently_reading(path=None) -> list[int]:
    """Tracked runs on On Deck right now — Continue reading first, then Next up, in
    the Deck's own order (kometa/ondeck.py; read-only)."""
    from kometa import ondeck
    path = path or DB_PATH
    out: list[int] = []
    for card in ondeck.continue_reading(path=path) + ondeck.up_next(path=path):
        sid = card.get("series_id")
        if sid and sid not in out:
            out.append(sid)
    return out


def seed_weights(path=None) -> dict[int, float]:
    """{tracked_series_id: weight} for every run that says something about you
    (zero-weight runs left out). Read live on every call — a rating added this
    minute counts this minute. Read-only on marks / read_progress / books / issue_status."""
    from kometa.marks import rated_series
    from kometa.auth import current_reader
    path = path or DB_PATH
    tier: dict[int, float] = {}

    def lift(sid, w):
        if sid is not None:
            tier[sid] = max(tier.get(sid, 0.0), w)
    with db._connect(path) as conn:
        owned_ids, _ = _owned(conn)
        for sid in owned_ids:
            lift(sid, W_OWNED)
        for r in conn.execute("""SELECT DISTINCT b.tracked_series_id FROM read_progress p JOIN books b ON b.id = p.book_id
                WHERE p.reader_id = ? AND p.completed = 1""", (current_reader(),)):
            lift(r[0], W_READ)
        for r in conn.execute("""SELECT target_id FROM marks WHERE reader_id = ? AND kind = 'series' AND favourite = 1
                UNION SELECT b.tracked_series_id FROM marks m JOIN books b ON b.id = m.target_id
                WHERE m.reader_id = ? AND m.kind = 'book' AND m.favourite = 1""", (current_reader(), current_reader())):
            lift(r[0], W_FAVOURITE)
    for sid in currently_reading(path):
        lift(sid, W_ONDECK)
    out = dict(tier)
    for sid, rating in rated_series(path).items():
        out[sid] = out.get(sid, 0.0) + W_RATING * (float(rating) - NEUTRAL_RATING)
    return {sid: round(w, 3) for sid, w in out.items() if w}


def _path_key(sp: str) -> tuple[str, int | None]:
    """'/comic-books/reviews/image-comics/ghost-pepper-(2025)' → ('ghost-pepper', 2025)."""
    seg = sp.rstrip("/").rsplit("/", 1)[-1].lower()
    m = re.search(r"-?\((\d{4})\)$", seg)
    slug = seg[:m.start()] if m else seg
    return re.sub(r"^the-", "", slug), int(m.group(1)) if m else None


def _owned(conn) -> tuple[set[int], dict[str, list[int | None]]]:
    """What's on disk: tracked runs with an owned issue or a book row, and
    {slug: [start years]} for those plus every shelf folder (untracked too).
    Ownership is the folder, never a catalogue's say-so."""
    from kometa.cbr import series_slug
    ids = {r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM issue_status WHERE owned = 1")}
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM books WHERE tracked_series_id IS NOT NULL")}
    keys: dict[str, list[int | None]] = {}

    def add(title, year):
        m = re.search(r"\((\d{4})\)", title or "")
        slug = re.sub(r"^the-", "", series_slug(title))
        if slug:
            keys.setdefault(slug, []).append(int(m.group(1)) if m else (int(year) if year else None))
    for r in conn.execute("SELECT id, title, year_began FROM tracked_series"):
        if r["id"] in ids:
            add(r["title"], r["year_began"])
    for r in conn.execute("SELECT title FROM shelf_series WHERE book_count > 0"):
        add(r["title"], None)
    return ids, keys


def _is_owned(sp: str, keys: dict[str, list[int | None]]) -> bool:
    """Same name and (a year missing, or starts within a year): when in doubt, it's
    yours — a missed recommendation costs less than recommending what's on the shelf."""
    slug, year = _path_key(sp)
    return any(y is None or year is None or abs(y - year) <= 1 for y in keys.get(slug, ()))


def _display_name(sp: str, stored: str | None) -> str:
    if stored:
        return stored
    slug, _ = _path_key(sp)
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def _newest(path) -> str | None:
    with db._connect(path) as conn:
        return conn.execute("""SELECT MAX(f) FROM (SELECT MAX(fetched_at) f FROM cbr_people_pages
                               UNION ALL SELECT MAX(fetched_at) FROM cbr_people)""").fetchone()[0]


def _cached(key, build):
    """An hour per key; every key carries the harvest's newest fetch, so a night's
    new pages (or a new rating, via the seeds in the key) make a fresh answer."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
    value = build()
    with _cache_lock:
        for k in [k for k, v in _cache.items() if time.time() - v[0] >= CACHE_TTL_S]:
            del _cache[k]
        _cache[key] = (time.time(), value)
    return value


def _load(path) -> dict:
    """Everything the ranking reads, once per harvest state: tens of thousands of
    rows grouped in SQL, centred in Python."""
    def build():
        with db._connect(path) as conn:
            owned_ids, owned_keys = _owned(conn)
            mean = {r[0]: r[1] for r in conn.execute("SELECT person, AVG(score) FROM cbr_people_scores GROUP BY person")}
            centred: dict[str, dict[str, float]] = {}
            for r in conn.execute("SELECT person, series_path, AVG(score) FROM cbr_people_scores GROUP BY person, series_path"):
                centred.setdefault(r[0], {})[r[1]] = r[2] - mean[r[0]]
            return {"linked": _linked(conn), "owned_ids": owned_ids, "owned_keys": owned_keys, "centred": centred,
                    "titles": {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tracked_series")},
                    "kind": {r[0]: r[1] for r in conn.execute("SELECT person, kind FROM cbr_people")},
                    "names": {r[0]: r[1] for r in conn.execute("SELECT series_path, name FROM cbr_people_series")}}
    return _cached(("load", path, _newest(path)), build)


def community(path=None, limit: int = 20) -> dict:
    """Runs you don't own, ranked by how people who share your taste scored them."""
    path = path or DB_PATH
    _ready(path)
    seeds = seed_weights(path)
    newest = _newest(path)
    value = _cached(("all", path, newest, limit, tuple(sorted(seeds.items()))),
                    lambda: _rank(path, seeds, limit, MIN_SEEDS))
    return {**value, "updated": newest}


def because(series_id: int, path=None, limit: int = 12) -> dict:
    """'Readers who liked THIS run also like…': one seed, everyone who scored it
    above their own mean. {seed: {series_id, title, series_path, supporters}, items}."""
    path = path or DB_PATH
    _ready(path)
    newest = _newest(path)
    value = _cached(("because", path, newest, int(series_id), limit),
                    lambda: _rank(path, {int(series_id): 1.0}, limit, 1))
    return {**value, "updated": newest}


def because_ondeck(path=None, limit: int = 12, rows: int = 3) -> list[dict]:
    """Up to `rows` 'Because you're reading X' rows, one per On Deck run that has
    enough harvested people (≥ MIN_SUPPORTERS liked it) and something to show."""
    path = path or DB_PATH
    out = []
    for sid in currently_reading(path):
        r = because(sid, path, limit)
        if r["seed"]["supporters"] >= MIN_SUPPORTERS and r["items"]:
            out.append(r)
            if len(out) >= rows:
                break
    return out


def _rank(path, seeds: dict[int, float], limit: int, min_seeds: int) -> dict:
    d = _load(path)
    linked, centred, kind, titles = d["linked"], d["centred"], d["kind"], d["titles"]
    seed_paths = {sp: sid for sp, sid in linked.items() if sid in seeds}
    mine = {sp for sp, sid in linked.items() if sid in d["owned_ids"]} | set(seed_paths)

    raw: dict[str, float] = {}
    for p, by in centred.items():
        hits = [(seeds[seed_paths[sp]], c) for sp, c in by.items() if sp in seed_paths]
        n = len(hits)
        if n < min_seeds or p not in kind:
            continue
        w = sum(sw * c for sw, c in hits) * n / (n + SHRINK_K)
        if w > 0:
            raw[p] = w
    pool = {k: sum(w for p, w in raw.items() if kind[p] == k) for k in BLEND}
    share_total = sum(BLEND[k] for k in BLEND if pool[k] > 0) or 1.0
    weight = {p: BLEND[kind[p]] / share_total * w / pool[kind[p]] for p, w in raw.items()}

    num: dict[str, float] = {}
    den: dict[str, float] = {}
    support: dict[str, list[str]] = {}
    for p, w in weight.items():
        for sp, c in centred[p].items():
            if sp in mine:
                continue
            num[sp] = num.get(sp, 0.0) + w * c
            den[sp] = den.get(sp, 0.0) + abs(w)
            if c > 0:
                support.setdefault(sp, []).append(p)
    ranked = []
    for sp, who in support.items():
        if len(who) < MIN_SUPPORTERS or _is_owned(sp, d["owned_keys"]):
            continue
        s = num[sp] / (den[sp] + LAMBDA)
        if s > 0:
            ranked.append((s, sp))
    ranked.sort(key=lambda t: (-t[0], t[1]))

    top = ranked[:limit]
    first_year = {}
    if top:
        with db._connect(path) as conn:
            qs = ",".join("?" * len(top))
            first_year = {r[0]: r[1] for r in conn.execute(
                f"""SELECT series_path, MIN(substr(date, 1, 4)) FROM cbr_people_scores
                    WHERE series_path IN ({qs}) AND date LIKE '____-__-__' GROUP BY series_path""",
                [sp for _, sp in top])}
    liked_seeds = [sp for sp in seed_paths if seeds[seed_paths[sp]] > 0]
    items = []
    for s, sp in top:
        who = support[sp]
        overlap: dict[str, int] = {}
        for p in who:
            for seed_sp in liked_seeds:
                if centred[p].get(seed_sp, 0) > 0:
                    overlap[seed_sp] = overlap.get(seed_sp, 0) + 1
        top_seeds = sorted(overlap, key=lambda x: (-overlap[x], -seeds[seed_paths[x]],
                                                   titles.get(seed_paths[x]) or ""))[:3]
        bt = [titles[seed_paths[x]] for x in top_seeds if titles.get(seed_paths[x])]
        pub = sp.split("/")[3]
        _, year = _path_key(sp)
        fy = first_year.get(sp)
        items.append({
            "series_path": sp, "name": _display_name(sp, d["names"].get(sp)),
            "year": year or (int(fy) if fy and fy.isdigit() else None),
            "publisher": pub, "publisher_name": _PUBLISHER_NAMES.get(pub) or pub.replace("-", " ").title(),
            "url": SITE + sp, "score": round(s, 4), "supporters": len(who),
            "critics": sum(1 for p in who if kind[p] == "critic"), "users": sum(1 for p in who if kind[p] == "user"),
            "because": bt, "because_ids": [seed_paths[x] for x in top_seeds],
            "label": f"Liked by {len(who)} readers who rate {' and '.join(bt[:2])} highly" if bt else None,
        })
    out = {"items": items, "seeds": len(seed_paths), "people": len(weight)}
    if len(seeds) == 1:
        sid = next(iter(seeds))
        sp = next((p for p, s in linked.items() if s == sid), None)
        out["seed"] = {"series_id": sid, "title": titles.get(sid), "series_path": sp,
                       "supporters": sum(1 for by in centred.values() if sp and by.get(sp, 0) > 0)}
    return out


@router.get("/api/discover/community")
def get_community(limit: int = 20):
    from kometa.community_covers import enrich           # a cover and a way in for each pick
    r = community(limit=max(1, min(int(limit), 100)))
    enrich(r.get("items", []), DB_PATH)
    return r


@router.get("/api/discover/community/because")
def get_because(series_id: int, limit: int = 12):
    from kometa.community_covers import enrich
    r = because(series_id, limit=max(1, min(int(limit), 100)))
    enrich(r.get("items", []), DB_PATH)
    return r


@router.get("/api/discover/community/because-ondeck")
def get_because_ondeck(limit: int = 12):
    from kometa.community_covers import enrich
    rows = because_ondeck(limit=max(1, min(int(limit), 100)))
    for r in rows:
        enrich(r.get("items", []), DB_PATH)
    return {"rows": rows}
