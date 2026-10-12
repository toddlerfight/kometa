"""Hardcover — readers' ratings for books, by ISBN (enrichment 1i-f).

Trades and graphic novels are books, and Hardcover (hardcover.app) is a reading
community with a public GraphQL API. Its terms let us show aggregate data when
it's credited ("average Hardcover rating"); it's the user's own token, and this
module only ever touches the catalogue (editions and their book) — never a
user, a library or a list (the token's scope is read:catalog).

API facts (docs: github.com/hardcoverapp/hardcover-docs, Getting-Started.mdx,
read 2026-10-12): POST https://api.hardcover.app/v1/graphql with an
'authorization: Bearer <token>' header; free plan 5,000 a day, 60 a minute,
burst 10; EACH TOP-LEVEL FIELD costs one request, so one `editions` query with
an `_in` list of ISBNs answers a whole batch for the price of one; max depth 3;
`_like`/`_regex` and friends are disabled; 30 s query timeout. Responses carry
X-RateLimit-Daily-Remaining / -Daily-Reset; a 429 carries Retry-After.

Ours: one polite.Limiter (1.2 s gap, 4,000 a day — the rest of the 5,000 is the
user's), ISBN batches of BATCH, a dated snapshot per ISBN in hardcover_record
(beside openlibrary_record, same shape of idea), copied to community_json
['hardcover'] on any issue carrying that ISBN. Asked again after RECHECK_DAYS.
"""
import logging
from datetime import datetime, timedelta, timezone

import kometa.db as db
from kometa.openlibrary import norm_isbn
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

API = "https://api.hardcover.app/v1/graphql"
BATCH = 25                     # ISBNs per request — one top-level field, one request against the limits
PER_TICK = 4                   # requests per 10-min tick
RECHECK_DAYS = 90              # ratings are a dated snapshot; misses are asked again too
DAILY_FLOOR = 250              # Hardcover's own count below this: rest till its reset (the token may be used elsewhere)
TEST_ISBN = "9781607066019"    # Saga Vol 1 — the Settings test's one light question

QUERY = """query KometaEditionsByIsbn($isbns: [String!]) {
  editions(where: {isbn_13: {_in: $isbns}}) {
    id
    isbn_13
    book { id slug title rating ratings_count }
  }
}"""
TEST_QUERY = """query KometaTest($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}, limit: 1) { id }
}"""

limiter = Limiter("Hardcover", min_interval_s=1.2, daily_cap=4000, timeout_s=35, refused_pause_s=3600,
                  user_agent="Kometa/1.0 (personal comic library; polite background harvest of book ratings by ISBN)")


class HardcoverRefused(SourceUnavailable):
    """The token was refused (401): nothing more until it's replaced."""


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS hardcover_record (
            isbn TEXT PRIMARY KEY, state TEXT, edition_id INTEGER, book_id INTEGER, slug TEXT, title TEXT,
            rating REAL, rating_count INTEGER, at TEXT)""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def isbn13(raw: str | None) -> str | None:
    """Any ISBN → its 13-digit form (Hardcover is asked by isbn_13 only, so one
    `_in` list does: an ISBN-10 is 978 + its first nine digits + a new check)."""
    n = norm_isbn(raw)
    if not n or len(n) == 13:
        return n
    core = "978" + n[:9]
    check = (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10
    return core + str(check)


def _token(path=None) -> str:
    t = (db.get_config(path or DB_PATH).get("hardcover_token") or "").strip()
    return t[7:].strip() if t.lower().startswith("bearer ") else t   # pasted with or without the 'Bearer '


def configured(path=None) -> bool:
    return bool(_token(path))


def _post(query: str, variables: dict, token: str) -> dict:
    """One GraphQL request through the one throttle. Returns `data`; raises
    HardcoverRefused on 401, SourceUnavailable on anything else that isn't an answer."""
    r = limiter.post(API, json={"query": query, "variables": variables},
                     headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
    _heed_daily(getattr(r, "headers", None) or {})
    if r.status_code == 401:
        raise HardcoverRefused("Hardcover refused the token")
    if r.status_code != 200:
        raise SourceUnavailable(f"Hardcover answered {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise SourceUnavailable("Hardcover's answer wasn't JSON") from None
    if body.get("errors"):
        msg = body["errors"][0].get("message") if isinstance(body["errors"][0], dict) else body["errors"][0]
        raise SourceUnavailable(f"Hardcover: {msg}")
    return body.get("data") or {}


def _heed_daily(headers) -> None:
    """Hardcover's own count of today's requests: below the floor, rest until its reset."""
    try:
        left = int(headers.get("X-RateLimit-Daily-Remaining"))
    except (TypeError, ValueError):
        return
    if left < DAILY_FLOOR:
        try:
            until = float(headers.get("X-RateLimit-Daily-Reset"))
        except (TypeError, ValueError):
            until = datetime.now(timezone.utc).timestamp() + 3600
        limiter._state["paused_until"] = max(limiter._state["paused_until"], until)
        logger.info(f"Hardcover: {left} requests left today by its count — resting till its reset")


def parse_editions(data: dict) -> dict[str, dict]:
    """isbn_13 → the best-rated book behind that ISBN (an ISBN can sit on more
    than one edition row; the one whose book has the most ratings wins)."""
    out: dict[str, dict] = {}
    for e in (data or {}).get("editions") or []:
        isbn = isbn13(e.get("isbn_13"))
        book = e.get("book") or {}
        if not isbn or not book:
            continue
        row = {"edition_id": e.get("id"), "book_id": book.get("id"), "slug": book.get("slug"),
               "title": book.get("title"), "rating_count": int(book.get("ratings_count") or 0),
               "rating": round(float(book["rating"]), 2) if book.get("rating") is not None and book.get("ratings_count") else None}
        if isbn not in out or row["rating_count"] > out[isbn]["rating_count"]:
            out[isbn] = row
    return out


def snapshot(row: dict) -> dict:
    """The dated snapshot an issue with this ISBN carries — credited, per Hardcover's terms."""
    return {"rating": row.get("rating"), "count": row.get("rating_count") or 0, "at": row.get("at"),
            "source": "hardcover", "url": f"https://hardcover.app/books/{row['slug']}" if row.get("slug") else None}


def candidates(path=None, limit: int = BATCH * PER_TICK) -> list[str]:
    """ISBN-13s not asked within RECHECK_DAYS: owned issues' first (pull list
    first), then trades on runs you own issues of, then every other trade."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import record
    record.ensure_tables(path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        asked = {r[0] for r in conn.execute("SELECT isbn FROM hardcover_record WHERE at >= ?", (cutoff,))}
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
    out: list[str] = []
    for isbn in raw:
        n = isbn13(isbn)
        if n and n not in asked and n not in out:
            out.append(n)
            if len(out) >= limit:
                break
    return out


def _store(isbn: str, found: dict | None, path) -> dict:
    row = {"isbn": isbn, "state": "found" if found else "none", "edition_id": None, "book_id": None,
           "slug": None, "title": None, "rating": None, "rating_count": 0, **(found or {}), "at": _now()}
    with db._connect(path) as conn:
        conn.execute(f"INSERT OR REPLACE INTO hardcover_record ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))
    return row


def _issues_with_isbn(isbn: str, path) -> list[tuple[int, float]]:
    with db._connect(path) as conn:
        rows = conn.execute("SELECT tracked_series_id, number, isbn FROM issue_record WHERE COALESCE(isbn, '') != ''").fetchall()
    return [(r[0], r[1]) for r in rows if isbn13(r[2]) == isbn]


def tick(requests_left: int = PER_TICK, path=None) -> int:
    """Scheduler tick: a few batches of ISBNs asked, each answer kept by ISBN
    and the rating snapshot merged into any issue record with that ISBN (beside
    LOCG's, never over it). A no-op with the toggle off, no token, a spent day
    or a pause. Returns ISBNs answered."""
    path = path or DB_PATH
    if not enabled("hardcover_enabled", path) or limiter.spent():
        return 0
    token = _token(path)
    if not token:
        return 0
    ensure_tables(path)
    from kometa import record
    pending = candidates(path, limit=BATCH * requests_left)
    done = 0
    try:
        for i in range(0, len(pending), BATCH):
            batch = pending[i:i + BATCH]
            found = parse_editions(_post(QUERY, {"isbns": batch}, token))
            for isbn in batch:
                row = _store(isbn, found.get(isbn), path)
                if row["state"] == "found":
                    for sid, n in _issues_with_isbn(isbn, path):
                        record.merge_community(sid, n, "hardcover", snapshot(row), path)
                done += 1
    except SourceUnavailable as e:
        logger.info(f"Hardcover stopped this tick: {e}")
    if done:
        logger.info(f"Hardcover: {done} ISBNs asked")
    return done


def test(path=None) -> tuple[bool, str]:
    """Settings' test: one light catalogue question (one edition's id by ISBN)."""
    token = _token(path)
    if not token:
        return False, "Not configured"
    try:
        data = _post(TEST_QUERY, {"isbn": TEST_ISBN}, token)
    except HardcoverRefused:
        return False, "Hardcover refused the token"
    except SourceUnavailable as e:
        return False, str(e)
    return True, "Hardcover answered" + (" (Saga Vol 1 found)" if data.get("editions") else "")


def get(isbn: str, path=None) -> dict | None:
    """What Hardcover said about an ISBN, if it's been asked."""
    path = path or DB_PATH
    ensure_tables(path)
    n = isbn13(isbn)
    if not n:
        return None
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM hardcover_record WHERE isbn = ?", (n,)).fetchone()
    return dict(r) if r else None


def status(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("""SELECT SUM(state = 'found') AS found, SUM(state = 'none') AS none,
                                   SUM(state = 'found' AND rating_count > 0) AS rated FROM hardcover_record""").fetchone()
    return {"found": r["found"] or 0, "none": r["none"] or 0, "rated": r["rated"] or 0,
            "budget": limiter.budget(), "enabled": enabled("hardcover_enabled", path), "configured": configured(path)}
