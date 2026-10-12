"""Open Library — readers' ratings and big covers for books, by ISBN (enrichment 1i).

Trades and graphic novels are books, and Open Library knows books by ISBN:
/isbn/{isbn}.json is the edition (it redirects to /books/OL…M.json) carrying
the work key, cover ids, page count and the Goodreads/LibraryThing ids;
{work}/ratings.json is the 1–5 average, count and histogram; and
covers.openlibrary.org/b/id/{id}.jpg with no size suffix is the original upload
(3008 px for Saga Vol 1 — the -L size is only 332 px). Shapes probed 2026-10-12
(docs/research/comic-sources-2026-10-12.md §2).

Polite by Open Library's own ask: no more than one request a second, a named
User-Agent, and a daily cap (2,000) well short of anything bulk. One ISBN's
answer is kept in openlibrary_record (a trade's ISBN can sit on several runs'
shelves) and re-asked after RECHECK_DAYS — ratings are a dated snapshot.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import kometa.db as db
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

BASE = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org/b/id/"
PER_TICK = 20                  # requests per 10-min tick (≤3 per ISBN); the daily cap ends the day first
RECHECK_DAYS = 90              # ratings move slowly on Open Library; a 'not there' is asked again too

limiter = Limiter("Open Library", min_interval_s=1.0, daily_cap=2000, timeout_s=30,
                  user_agent="Kometa/1.0 (personal comic library; polite background harvest, 1 req/s)")


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS openlibrary_record (
            isbn TEXT PRIMARY KEY, state TEXT, edition_key TEXT, work_key TEXT, goodreads_id TEXT,
            cover_id INTEGER, pages INTEGER, rating REAL, rating_count INTEGER, histogram_json TEXT, at TEXT)""")


def norm_isbn(raw: str | None) -> str | None:
    """'978-1-60706-601-9' → '9781607066019'; ISBN-10s kept as they are (Open
    Library answers both). Anything else isn't an ISBN."""
    s = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    if len(s) == 13 and s.isdigit() and s[:3] in ("978", "979"):
        return s
    if len(s) == 10 and s[:9].isdigit():
        return s
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def candidates(path=None, limit: int = 50) -> list[str]:
    """ISBNs not asked within RECHECK_DAYS: owned issues' ISBNs first (pull
    list first), then trades on runs you own issues of, then every other trade."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import record
    record.ensure_tables(path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        asked = {r[0] for r in conn.execute("SELECT isbn FROM openlibrary_record WHERE at >= ?", (cutoff,))}
        raw = [r[0] for r in conn.execute("""
            SELECT r.isbn FROM issue_record r
            JOIN issue_status i ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
            JOIN tracked_series s ON s.id = r.tracked_series_id
            WHERE i.owned = 1 AND COALESCE(r.isbn, '') != '' AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number""")]
        raw += [r[0] for r in conn.execute("""
            SELECT t.isbn FROM trade_record t JOIN tracked_series s ON s.id = t.tracked_series_id
            WHERE COALESCE(t.isbn, '') != ''
            ORDER BY s.on_pull_list DESC,
                     (SELECT COUNT(*) FROM issue_status i WHERE i.tracked_series_id = s.id AND i.owned = 1) > 0 DESC,
                     t.tracked_series_id, t.store_date""")]
    out = []
    for isbn in raw:
        n = norm_isbn(isbn)
        if n and n not in asked and n not in out:
            out.append(n)
            if len(out) >= limit:
                break
    return out


def parse_edition(d: dict) -> dict:
    ids = d.get("identifiers") or {}
    gr = (ids.get("goodreads") or [None])[0]
    covers = [c for c in d.get("covers") or [] if isinstance(c, int) and c > 0]   # -1 = 'no cover' on OL
    works = [w.get("key") for w in d.get("works") or [] if isinstance(w, dict) and w.get("key")]
    return {"edition_key": d.get("key"), "work_key": works[0] if works else None,
            "goodreads_id": str(gr).split("-", 1)[0] if gr else None,
            "cover_id": covers[0] if covers else None, "pages": d.get("number_of_pages")}


def parse_ratings(d: dict) -> dict:
    s = (d or {}).get("summary") or {}
    count = int(s.get("count") or 0)
    avg = s.get("average")
    return {"rating": round(float(avg), 2) if count and avg is not None else None, "count": count,
            "histogram": {str(k): int(v) for k, v in ((d or {}).get("counts") or {}).items()}}


def snapshot(row: dict) -> dict:
    """The dated snapshot the record carries for an issue with this ISBN."""
    return {"rating": row.get("rating"), "count": row.get("rating_count") or 0, "at": row.get("at"),
            "source": "openlibrary"}


def ask(isbn: str, spend=lambda: True, have=lambda url: False) -> dict:
    """One ISBN: the edition, its work's ratings, the original cover's bytes
    (unless `have` says the store already holds that URL — a recheck only
    wants the ratings). {'state': 'none'} when Open Library hasn't the edition."""
    if not spend():
        raise SourceUnavailable("Open Library: this tick's requests are spent")
    r = limiter.get(f"{BASE}/isbn/{isbn}.json")                      # redirects to /books/…: one ask, one slot
    if r.status_code == 404:
        return {"state": "none"}
    if r.status_code != 200:
        raise SourceUnavailable(f"Open Library answered {r.status_code}")
    out = {"state": "found", **parse_edition(r.json())}
    out.update(rating=None, count=0, histogram={})
    if out["work_key"]:
        if not spend():
            raise SourceUnavailable("Open Library: this tick's requests are spent")
        rr = limiter.get(f"{BASE}{out['work_key']}/ratings.json")
        if rr.status_code == 200:
            out.update(parse_ratings(rr.json()))
    out["cover"] = None
    if out["cover_id"] and not have(f"{COVERS}{out['cover_id']}.jpg"):
        if not spend():
            raise SourceUnavailable("Open Library: this tick's requests are spent")
        url = f"{COVERS}{out['cover_id']}.jpg"
        cr = limiter.get(url)
        ctype = (cr.headers.get("Content-Type") or "") if hasattr(cr, "headers") else ""
        if cr.status_code == 200 and cr.content and ctype.startswith("image/"):
            out["cover"] = (url, cr.content)
    return out


def _store(isbn: str, found: dict, path) -> dict:
    row = {"isbn": isbn, "state": found["state"], "edition_key": found.get("edition_key"),
           "work_key": found.get("work_key"), "goodreads_id": found.get("goodreads_id"),
           "cover_id": found.get("cover_id"), "pages": found.get("pages"), "rating": found.get("rating"),
           "rating_count": found.get("count") or 0, "histogram_json": json.dumps(found.get("histogram") or {}),
           "at": _now()}
    with db._connect(path) as conn:
        conn.execute(f"INSERT OR REPLACE INTO openlibrary_record ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))
    return row


def _issues_with_isbn(isbn: str, path) -> list[tuple[int, float]]:
    with db._connect(path) as conn:
        rows = conn.execute("SELECT tracked_series_id, number, isbn FROM issue_record WHERE COALESCE(isbn, '') != ''").fetchall()
    return [(r[0], r[1]) for r in rows if norm_isbn(r[2]) == isbn]


def tick(budget: int = PER_TICK, path=None) -> int:
    """Scheduler tick: a few ISBNs asked, each answer kept by ISBN, the rating
    snapshot merged into any issue record with that ISBN (beside LOCG's, never
    over it), the cover into the image store at isbn/<isbn>/ol. A no-op with
    the toggle off, a spent day or a pause. Returns ISBNs answered."""
    path = path or DB_PATH
    if not enabled("openlibrary_enabled", path) or limiter.spent():
        return 0
    ensure_tables(path)
    from kometa import images, record
    left = [budget]

    def spend() -> bool:
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    done = 0
    try:
        for isbn in candidates(path, limit=budget):
            if left[0] <= 0:
                break
            key = images.isbn_key(isbn, "ol")

            def have(url, key=key) -> bool:
                held = images.get(key, path)
                return bool(held and held.get("url") == url and images.local_path(key, path))

            found = ask(isbn, spend, have)
            if found.get("cover"):
                url, data = found["cover"]
                images.store_bytes(key, data, url, "openlibrary", path)
            row = _store(isbn, found, path)
            if found["state"] == "found":
                for sid, n in _issues_with_isbn(isbn, path):
                    record.merge_community(sid, n, "openlibrary", snapshot(row), path)
            done += 1
    except SourceUnavailable as e:
        logger.info(f"Open Library stopped this tick: {e}")
    if done:
        logger.info(f"Open Library: {done} ISBNs asked")
    return done


def get(isbn: str, path=None) -> dict | None:
    """What Open Library said about an ISBN, if it's been asked."""
    path = path or DB_PATH
    ensure_tables(path)
    n = norm_isbn(isbn)
    if not n:
        return None
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM openlibrary_record WHERE isbn = ?", (n,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["histogram"] = json.loads(d.pop("histogram_json") or "{}")
    return d


def status(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("""SELECT SUM(state = 'found') AS found, SUM(state = 'none') AS none,
                                   SUM(state = 'found' AND rating_count > 0) AS rated,
                                   SUM(state = 'found' AND cover_id IS NOT NULL) AS covers
                            FROM openlibrary_record""").fetchone()
    return {"found": r["found"] or 0, "none": r["none"] or 0, "rated": r["rated"] or 0, "covers": r["covers"] or 0,
            "budget": limiter.budget(), "enabled": enabled("openlibrary_enabled", path)}
