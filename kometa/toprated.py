"""Top rated: the community's scores, already in the record, turned into a ranking.

Two readers of the same numbers, no network anywhere:
  series(id)  — one owned run's issues, best first (the series page's ★ sort).
  picks()     — On Deck's 'Community picks': your best-rated runs you haven't
                finished reading.

The numbers are what the workers already merged into issue_record.community_json:
ComicBookRoundup's critic and reader scores (out of 10), LOCG's average (out of 5),
and once in a while Hardcover / Open Library (out of 5). Everything goes to /10.

The formula, so nobody has to reverse-engineer it at 2am:
  1. Per issue, each source is a (score/10, votes) pair. Votes are capped at CAP:
     past that a source is sure enough, and LOCG's 5,000-strong crowd doesn't get
     to shout over eight critics who wrote actual reviews.
     raw = Σ score·min(votes, CAP) / Σ min(votes, CAP);  n = Σ min(votes, CAP)
  2. Shrink toward a prior (a Bayesian average): (n·raw + m·prior) / (n + m).
     m is how many votes the prior is worth. One 10/10 review lands barely above
     the prior; thirty of them mean it.
  3. A run's score pools its issues the same way (raw weighted by each issue's n),
     shrunk toward the mean of every rated issue in the library (SERIES_M).
     An issue's prior is its own run's shrunk score — 'great for THIS book' —
     and an issue under MIN_VOTES real votes isn't ranked at all.
"""
import json

from fastapi import APIRouter

import kometa.db as db
from kometa.auth import current_reader
router = APIRouter()
DB_PATH = db.DB_PATH

CAP = 25              # votes per source that count; past this, more people don't make it truer
ISSUE_M = 10          # an issue's prior is worth ten votes
SERIES_M = 30         # a run's prior is worth thirty
MIN_VOTES = 3         # fewer real votes than this and an issue isn't ranked
FALLBACK_MEAN = 7.0   # an empty library's idea of 'average' — only ever seen by tests and day zero
PICKS_LIMIT = 24      # On Deck's ROW_LIMIT
PICKS_MIN = 4         # a discovery row with fewer cards isn't a row (ondeck.DISCOVERY_MIN)


def sources(c: dict | None) -> list[tuple[float, int]]:
    """(score out of 10, votes) for every source that rated this issue."""
    if not c:
        return []
    out = []

    def add(v, n, scale):
        try:
            v, n = float(v), int(n or 0)
        except (TypeError, ValueError):
            return
        if n > 0:
            out.append((v * scale, n))

    if c.get("rating") is not None:
        add(c["rating"], c.get("ratings"), 2)                 # LOCG: 0–5
    cbr = c.get("cbr") or {}
    if cbr.get("critic") is not None:
        add(cbr["critic"], cbr.get("critic_reviews"), 1)
    if cbr.get("user") is not None:
        add(cbr["user"], cbr.get("user_reviews"), 1)
    for k in ("hardcover", "openlibrary"):
        b = c.get(k) or {}
        if b.get("rating") is not None:
            add(b["rating"], b.get("count"), 2)
    return out


def pool(pairs: list[tuple[float, int]]) -> tuple[float | None, int, int]:
    """(raw weighted mean, capped weight, real votes). Step 1 above."""
    w = sum(min(n, CAP) for _, n in pairs)
    if not w:
        return None, 0, 0
    return sum(v * min(n, CAP) for v, n in pairs) / w, w, sum(n for _, n in pairs)


def shrink(raw: float | None, n: float, prior: float, m: float) -> float:
    """Step 2: the Bayesian average. No votes = the prior."""
    return prior if raw is None or n <= 0 else (n * raw + m * prior) / (n + m)


def _rated_issues(conn) -> dict[int, dict[float, tuple]]:
    """{series_id: {number: (raw, weight, votes)}} for every issue with a score.
    The whole library every time: ~2.5k small JSON blobs, a few ms, and the
    global mean needs them all anyway."""
    out: dict[int, dict[float, tuple]] = {}
    for r in conn.execute("SELECT tracked_series_id, number, community_json FROM issue_record WHERE community_json IS NOT NULL"):
        try:
            c = json.loads(r["community_json"] or "null")
        except (TypeError, ValueError):
            continue
        raw, w, votes = pool(sources(c if isinstance(c, dict) else None))
        if raw is not None:
            out.setdefault(r["tracked_series_id"], {})[r["number"]] = (raw, w, votes)
    return out


def _global_mean(rated: dict[int, dict[float, tuple]]) -> float:
    """Every rated issue in the library, pooled — the prior a run starts from."""
    rows = [x for issues in rated.values() for x in issues.values()]
    w = sum(x[1] for x in rows)
    return sum(x[0] * x[1] for x in rows) / w if w else FALLBACK_MEAN


def _series_score(issues: dict[float, tuple], prior: float) -> tuple[float, int, int]:
    """Step 3: (shrunk run score, pooled weight, real votes)."""
    w = sum(x[1] for x in issues.values())
    raw = sum(x[0] * x[1] for x in issues.values()) / w if w else None
    return shrink(raw, w, prior, SERIES_M), w, sum(x[2] for x in issues.values())


def _owned(conn, series_id: int) -> bool:
    return conn.execute("SELECT 1 FROM issue_status WHERE tracked_series_id = ? AND owned = 1 LIMIT 1",
                        (series_id,)).fetchone() is not None


def series(series_id: int, path=None) -> dict:
    """One run's rated issues, best first. A run you don't own gets nothing —
    ownership is the folder on disk, and the sort is for what's on the shelf."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        if not _owned(conn, series_id):
            return {"owned": False, "score": None, "issues": []}
        rated = _rated_issues(conn)
        issues = rated.get(series_id, {})
        if not issues:
            return {"owned": True, "score": None, "issues": []}
        run, _, votes = _series_score(issues, _global_mean(rated))
        owned = {r[0] for r in conn.execute(
            "SELECT number FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (series_id,))}
    ranked = [{"number": num, "score": round(shrink(raw, w, run, ISSUE_M), 2), "raw": round(raw, 2),
               "votes": v, "owned": num in owned}
              for num, (raw, w, v) in issues.items() if v >= MIN_VOTES]
    ranked.sort(key=lambda x: (-x["score"], -x["votes"], x["number"]))
    return {"owned": True, "score": round(run, 2), "votes": votes, "issues": ranked}


def _read_state(conn, ids: list[int], reader_id: str) -> dict[int, tuple[int, int]]:
    """{series_id: (books read, books)} — numbered books only when the run has
    any (trades and oddities don't decide 'finished', same as On Deck's Next)."""
    by: dict[int, list] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for b in conn.execute(f"""
            SELECT b.tracked_series_id AS sid, b.number, COALESCE(p.completed, 0) AS done FROM books b
            LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE b.tracked_series_id IN ({",".join("?" * len(chunk))})""", (reader_id, *chunk)):
            by.setdefault(b["sid"], []).append(b)
    out = {}
    for sid, books in by.items():
        base = [b for b in books if b["number"] is not None] or books
        out[sid] = (sum(1 for b in base if b["done"]), len(base))
    return out


def picks(reader_id=None, path=None, limit: int = PICKS_LIMIT) -> list[dict]:
    """Your owned runs, by community score, minus the ones you've read to the
    end. Unread and part-read both stay; part-read wins a tie (you liked it
    enough to start). Fewer than PICKS_MIN and there's no row."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rated = _rated_issues(conn)
        if not rated:
            return []
        prior = _global_mean(rated)
        runs = {r["id"]: r for r in conn.execute(f"""
            SELECT t.id, t.title, t.publisher FROM tracked_series t
            WHERE t.id IN ({",".join("?" * len(rated))}) AND COALESCE(t.kind, 'series') != 'arc'
              AND EXISTS (SELECT 1 FROM issue_status i WHERE i.tracked_series_id = t.id AND i.owned = 1)""",
            tuple(rated))}
        reads = _read_state(conn, list(runs), reader_id)
    out = []
    for sid, r in runs.items():
        score, _, votes = _series_score(rated[sid], prior)
        if votes < MIN_VOTES:
            continue
        done, books = reads.get(sid, (0, 0))
        if books and done >= books:
            continue                                   # read to the end: not a pick, a memory
        out.append({"series_id": sid, "title": r["title"], "publisher": r["publisher"],
                    "score": round(score, 2), "votes": votes, "read": done, "books": books})
    out.sort(key=lambda x: (-x["score"], -(x["read"] > 0), -x["votes"], x["title"] or ""))
    out = out[:limit]
    return out if len(out) >= PICKS_MIN else []


@router.get("/api/series/{series_id}/top-rated")
def top_rated(series_id: int):
    return series(series_id)


@router.get("/api/ondeck/picks")
def community_picks():
    return {"picks": picks()}
