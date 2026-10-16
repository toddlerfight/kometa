"""ComicBookRoundup — critics' and readers' scores per issue (enrichment 1i).

The only critic aggregate there is (docs/research/comic-sources-2026-10-12.md
§2). No API, plain HTML, no Cloudflare. The useful find from the shape probe
(2026-10-12): a run's SERIES page — /comic-books/reviews/{publisher}/{series} —
lists every issue with its critic score, user score and both review counts, so
one page answers a whole run. Issue pages are never needed.

Slugs: the publisher ('marvel-comics', 'image-comics', 'boom-studios' …), and
the series name lower-cased with spaces as hyphens, ':' dropped, '.' kept, and
the start year in parentheses when the publisher has had more than one run of
that name ('amazing-spider-man-(2025)', 'alien-vs.-x-men-(2026)', plain
'saga'). Matching is conservative: a page is ours only when its heading is our
run's name AND its date range starts within a year of ours. Anything else —
no year to check, a different start, a different name — is no match, never a
guess. At most two URLs are tried per run.

Polite: one page every 3 s, 500 a day, through one throttle. A linked run is
re-read after RELINK_DAYS (scores are a dated snapshot); a run not found is
asked again after RETRY_DAYS.
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import kometa.db as db
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

BASE = "https://comicbookroundup.com/comic-books/reviews/"
PER_TICK = 4                   # pages per 10-min tick
RELINK_DAYS = 30
RETRY_DAYS = 90

limiter = Limiter("ComicBookRoundup", min_interval_s=3.0, daily_cap=500, timeout_s=30)

# publisher as the folder/catalogue says it → CBR's slug (seen on the site 2026-10-12)
_PUBLISHERS = (("marvel", "marvel-comics"), ("dc", "dc-comics"), ("vertigo", "dc-comics"),
               ("image", "image-comics"), ("boom", "boom-studios"), ("dark horse", "dark-horse-comics"),
               ("idw", "idw-publishing"), ("dynamite", "dynamite"), ("mad cave", "mad-cave-studios"))
_YEAR_RE = re.compile(r"\((\d{4})\)")


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS cbr_series (
            tracked_series_id INTEGER PRIMARY KEY, state TEXT, url TEXT, name TEXT, start_year INTEGER,
            critic REAL, critic_reviews INTEGER, user_score REAL, user_reviews INTEGER, issues INTEGER, at TEXT)""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def publisher_slug(publisher: str | None) -> str | None:
    p = (publisher or "").strip().lower()
    if not p:
        return None
    for key, slug in _PUBLISHERS:
        if re.search(rf"\b{re.escape(key)}\b", p):
            return slug
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", p)).strip("-") or None


def series_slug(title: str | None) -> str:
    """'The Amazing Spider-Man (2025)' → 'amazing-spider-man'; 'Miles Morales:
    Spider-Man' → 'miles-morales-spider-man'; 'Alien Vs. X-Men' → 'alien-vs.-x-men'."""
    t = _YEAR_RE.sub("", title or "").strip()
    t = re.sub(r"^\s*(?:-\s*|\d{1,2}\s*-\s+)", "", t).strip().strip("-").strip()
    t = re.sub(r"\b(the deluxe edition|deluxe edition|omnibus|(?:\d+(?:th|st|nd|rd)\s+)?anniversary edition|vol(?:\.|ume)?\s*\d+|gn)\b.*$", "", t, flags=re.I).strip(" -:")
    t = re.sub(r"\s+-\s+", ": ", t)                         # a folder's 'Dune- House' / ' - ' is a colon
    t = re.sub(r"^\s*the\s+", "", t, flags=re.I).lower()
    t = re.sub(r"[^a-z0-9 .\-]", "", t)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", t.strip())).strip("-")


def year_hint(s: dict, path=None) -> int | None:
    """The run's start year: the folder's '(YYYY)', else the catalogue's
    year_began, else the earliest owned issue's date, else the linked Metron series start year."""
    from kometa.harvest import _year_hint
    y = _year_hint(s) or s.get("year_began")
    if y:
        return int(y)
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("""SELECT MIN(substr(store_date, 1, 4)) FROM issue_status
            WHERE tracked_series_id = ? AND owned = 1 AND store_date LIKE '____-%'""", (s["id"],)).fetchone()
        if r and r[0]:
            return int(r[0])
        mid = s.get("metron_series_id")
        if mid:
            chk = conn.execute("SELECT metron_checked_at FROM tracked_series WHERE id = ?", (s["id"],)).fetchone()
            checked_at = chk[0] if chk else None
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            if not checked_at or checked_at < cutoff:
                from kometa import metron_client
                try:
                    d = metron_client.series_detail(mid)
                    now_str = _now()
                    if d.get("year"):
                        yr = int(d["year"])
                        conn.execute("UPDATE tracked_series SET year_began = ?, metron_checked_at = ? WHERE id = ?", (yr, now_str, s["id"]))
                        s["year_began"] = yr
                        return yr
                    else:
                        conn.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (now_str, s["id"]))
                except Exception:
                    pass
    return None


def candidate_urls(s: dict, year: int | None) -> list[str]:
    pub, slug = publisher_slug(s.get("publisher")), series_slug(s.get("title"))
    if not pub or not slug or not year:
        return []
    return [f"{BASE}{pub}/{slug}-({year})", f"{BASE}{pub}/{slug}"]


# --- the series page ----------------------------------------------------------------
def _num(text: str | None) -> float | None:
    try:
        return float(text) if text and text.strip().upper() != "N/A" else None
    except ValueError:
        return None


def _count(text: str) -> int:
    m = re.search(r"(\d[\d,]*)\s+Reviews?", text or "")
    return int(m.group(1).replace(",", "")) if m else 0


def _score_block(chunk: str, user: bool) -> tuple[float | None, int]:
    """The score <span> and its 'N Reviews' from one rating block."""
    cls = r'class="review user[^"]*"' if user else r'class="review(?! user)[^"]*"'
    m = re.search(cls + r"\s*>\s*<span>([^<]*)</span>", chunk)
    if not m:
        return None, 0
    rest = chunk[m.end():]
    nxt = re.search(r'class="review[ "]', rest)
    return _num(m.group(1)), _count(rest[:nxt.start()] if nxt else rest)


def parse_series_page(page: str) -> dict | None:
    """{name, start_year, critic, critic_reviews, user, user_reviews,
    issues: {number: {...}}}. None when it isn't a series page."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page or "", re.S)
    if not h1:
        return None
    name = html.unescape(re.sub(r"<[^>]+>", "", h1.group(1))).strip()
    head = page[:h1.start()]
    sub = head[head.rfind("issue-subhead"):] if "issue-subhead" in head else ""
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", re.sub(r"<a[^>]*>.*?</a>", "", sub, flags=re.S))
    after = page[h1.end():]
    table_at = after.find("<tbody")
    summary = after[:table_at if table_at >= 0 else len(after)]
    sect = summary[summary.find("review-section"):] if "review-section" in summary else ""
    critic, critic_n = _score_block(sect, user=False)
    user, user_n = _score_block(sect, user=True)
    issues = {}
    if table_at >= 0:
        body = after[table_at:after.find("</tbody>", table_at) if "</tbody>" in after[table_at:] else len(after)]
        for row in body.split("<tr")[1:]:
            href = re.search(r'href="/comic-books/reviews/[^"/]+/[^"/]+/([^"/]+)"', row)
            if not href or not re.fullmatch(r"\d+(?:\.\d+)?", href.group(1)):
                continue                                   # annuals, one-shots ('dragon-man-1'): not a number of ours
            c_at, u_at = row.find("critic-rating"), row.find("user-rating")
            c_chunk = row[c_at:u_at] if c_at >= 0 and u_at > c_at else (row[c_at:] if c_at >= 0 else "")
            u_chunk = row[u_at:] if u_at >= 0 else ""
            ic, icn = _score_block(c_chunk, user=False)
            iu, iun = _score_block(u_chunk, user=True)
            issues[float(href.group(1))] = {"critic": ic, "critic_reviews": icn, "user": iu, "user_reviews": iun,
                                            "path": href.group(0)[6:-1]}
    return {"name": name, "start_year": int(years[0]) if years else None, "critic": critic,
            "critic_reviews": critic_n, "user": user, "user_reviews": user_n, "issues": issues}


def matches(s: dict, page: dict | None, year: int | None) -> bool:
    """Ours only when the heading is our run's name and the page's run starts
    within a year of ours. No start year on the page = unsure = no."""
    from kometa.harvest import _gcd_name
    if not page or not year or not page.get("start_year"):
        return False
    raw_title = s.get("title") or ""
    clean_title = re.sub(r"\b(the deluxe edition|deluxe edition|omnibus|(?:\d+(?:th|st|nd|rd)\s+)?anniversary edition|vol(?:\.|ume)?\s*\d+|gn)\b.*$", "", raw_title, flags=re.I).strip(" -:")
    name_match = _gcd_name(page["name"]) == _gcd_name(raw_title) or (bool(clean_title) and _gcd_name(page["name"]) == _gcd_name(clean_title))
    return name_match and abs(page["start_year"] - year) <= 1


# --- the worker -----------------------------------------------------------------------
def candidates(path=None, limit: int = PER_TICK) -> list[dict]:
    """Runs with owned issues that CBR hasn't been asked about lately. Pull list
    first, then most owned."""
    path = path or DB_PATH
    ensure_tables(path)
    relink = (datetime.now(timezone.utc) - timedelta(days=RELINK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    retry = (datetime.now(timezone.utc) - timedelta(days=RETRY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        ids = [r[0] for r in conn.execute("""
            SELECT s.id FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            LEFT JOIN cbr_series c ON c.tracked_series_id = s.id
            WHERE i.owned = 1 AND (s.kind IS NULL OR s.kind != 'arc')
              AND (c.tracked_series_id IS NULL
                   OR (c.state = 'linked' AND c.at < ?) OR (c.state != 'linked' AND c.at < ?))
            GROUP BY s.id ORDER BY s.on_pull_list DESC, COUNT(*) DESC, s.id LIMIT ?""", (relink, retry, limit))]
    return [s for s in (db.get_series_by_id(i, path) for i in ids) if s]


def _save(sid: int, state: str, url: str | None, page: dict | None, path):
    p = page or {}
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO cbr_series (tracked_series_id, state, url, name, start_year, critic,
            critic_reviews, user_score, user_reviews, issues, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (sid, state, url, p.get("name"), p.get("start_year"), p.get("critic"), p.get("critic_reviews"),
                      p.get("user"), p.get("user_reviews"), len(p.get("issues") or {}), _now()))


def _write_issues(sid: int, page: dict, path) -> int:
    """Each issue of ours on the page — owned, or already in the record — gets
    community_json['cbr'], beside LOCG's fields (record.merge_community)."""
    from kometa import record
    with db._connect(path) as conn:
        ours = {r[0] for r in conn.execute("SELECT number FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,))}
        ours |= {r[0] for r in conn.execute("SELECT number FROM issue_record WHERE tracked_series_id = ?", (sid,))}
    at, n = _now(), 0
    for num in sorted(ours):
        hit = page["issues"].get(float(num))
        if not hit:
            continue
        snap = {k: hit[k] for k in ("critic", "critic_reviews", "user", "user_reviews")}
        snap.update(url="https://comicbookroundup.com" + hit["path"], at=at, source="comicbookroundup")
        record.merge_community(sid, num, "cbr", snap, path)
        n += 1
    return n


def tick(budget: int = PER_TICK, path=None) -> int:
    """Scheduler tick: a few runs' series pages, matched conservatively, scores
    into the record. A no-op with the toggle off, a spent day or a pause.
    Returns issues scored."""
    path = path or DB_PATH
    if not enabled("cbr_enabled", path) or limiter.spent():
        return 0
    ensure_tables(path)
    left, scored = budget, 0
    try:
        for s in candidates(path, limit=budget):
            year = year_hint(s, path)
            urls = candidate_urls(s, year)
            if not urls:
                _save(s["id"], "unsure", None, None, path)     # no publisher or year to check against: no guess
                continue
            hit, tried = None, 0
            for url in urls:
                if left <= 0:
                    break
                left -= 1
                tried += 1
                r = limiter.get(url)
                if r.status_code != 200:
                    continue
                page = parse_series_page(r.text)
                if matches(s, page, year):
                    hit = (url, page)
                    break
            if hit:
                _save(s["id"], "linked", hit[0], hit[1], path)
                n = _write_issues(s["id"], hit[1], path)
                scored += n
                logger.info(f"ComicBookRoundup: {s['title']!r} → {hit[0]} ({n} issues)")
            elif tried == len(urls):
                _save(s["id"], "none", None, None, path)
            else:
                break                                      # ran out mid-run: ask the rest next tick
            if left <= 0:
                break
    except SourceUnavailable as e:
        logger.info(f"ComicBookRoundup stopped this tick: {e}")
    return scored


def get_series(series_id: int, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM cbr_series WHERE tracked_series_id = ?", (series_id,)).fetchone()
    return dict(r) if r else None


def status(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        links = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM cbr_series GROUP BY state")}
        issues = conn.execute("""SELECT COUNT(*) FROM issue_record WHERE community_json LIKE '%"cbr": {%'""").fetchone()[0]
    return {"linked": links.get("linked", 0), "none": links.get("none", 0) + links.get("unsure", 0),
            "issues": issues, "budget": limiter.budget(), "enabled": enabled("cbr_enabled", path)}
