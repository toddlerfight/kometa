"""Favourites and ratings: reading state that hangs off the book or series row.

Spec (docs/reader-spec.md, 'Favourites and ratings'): a star on anything, a 1–5
rating on an issue from the modal and the reader's end screen, a series rating
derived from its issues when it has none of its own. Keyed by reader id like
all reading state; survives renames and re-downloads because it's keyed by row
id, not path. Ratings feed the Because-you-read anchors: what you rated highly
leads what you merely own.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
READER_ID = "me"
KINDS = ("book", "series")


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS marks (
            reader_id  TEXT NOT NULL, kind TEXT NOT NULL, target_id INTEGER NOT NULL,
            favourite  INTEGER NOT NULL DEFAULT 0, rating INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (reader_id, kind, target_id))""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_mark(kind: str, target_id: int, path=None, reader_id=READER_ID) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT favourite, rating FROM marks WHERE reader_id = ? AND kind = ? AND target_id = ?",
                         (reader_id, kind, target_id)).fetchone()
    return {"favourite": bool(r["favourite"]), "rating": r["rating"]} if r else {"favourite": False, "rating": None}


def set_mark(kind: str, target_id: int, favourite: bool | None = None, rating: int | None = None,
             clear_rating: bool = False, path=None, reader_id=READER_ID) -> dict:
    """Change what's given, keep the rest. rating 1–5; clear_rating drops it."""
    path = path or DB_PATH
    if kind not in KINDS:
        raise ValueError(kind)
    if rating is not None and not 1 <= int(rating) <= 5:
        raise ValueError("rating is 1–5")
    cur = get_mark(kind, target_id, path, reader_id)
    fav = cur["favourite"] if favourite is None else bool(favourite)
    rat = None if clear_rating else (cur["rating"] if rating is None else int(rating))
    with db._connect(path) as conn:
        if not fav and rat is None:
            conn.execute("DELETE FROM marks WHERE reader_id = ? AND kind = ? AND target_id = ?", (reader_id, kind, target_id))
        else:
            conn.execute("""INSERT INTO marks (reader_id, kind, target_id, favourite, rating, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(reader_id, kind, target_id) DO UPDATE SET favourite = excluded.favourite,
                rating = excluded.rating, updated_at = excluded.updated_at""",
                         (reader_id, kind, target_id, int(fav), rat, _now()))
    return {"favourite": fav, "rating": rat}


def marks_for(kind: str, path=None, reader_id=READER_ID) -> dict[int, dict]:
    """{target_id: {favourite, rating}} for everything marked of one kind."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return {r["target_id"]: {"favourite": bool(r["favourite"]), "rating": r["rating"]} for r in conn.execute(
            "SELECT target_id, favourite, rating FROM marks WHERE reader_id = ? AND kind = ?", (reader_id, kind))}


def series_rating(series_id: int, path=None, reader_id=READER_ID) -> dict:
    """The series' own rating, else the mean of its rated issues (one decimal)."""
    path = path or DB_PATH
    own = get_mark("series", series_id, path, reader_id)
    with db._connect(path) as conn:
        rows = conn.execute("""SELECT m.rating FROM marks m JOIN books b ON b.id = m.target_id
            WHERE m.reader_id = ? AND m.kind = 'book' AND m.rating IS NOT NULL AND b.tracked_series_id = ?""",
                            (reader_id, series_id)).fetchall()
    derived = round(sum(r["rating"] for r in rows) / len(rows), 1) if rows else None
    return {"favourite": own["favourite"], "rating": own["rating"], "rating_derived": derived, "rated_issues": len(rows)}


def rated_series(path=None, reader_id=READER_ID) -> dict[int, float]:
    """{series_id: rating} — own rating, else derived — for ranking anchors."""
    path = path or DB_PATH
    out = {sid: float(m["rating"]) for sid, m in marks_for("series", path, reader_id).items() if m["rating"]}
    with db._connect(path) as conn:
        for r in conn.execute("""SELECT b.tracked_series_id AS sid, AVG(m.rating) AS r FROM marks m JOIN books b ON b.id = m.target_id
            WHERE m.reader_id = ? AND m.kind = 'book' AND m.rating IS NOT NULL AND b.tracked_series_id IS NOT NULL
            GROUP BY b.tracked_series_id""", (reader_id,)):
            out.setdefault(r["sid"], round(r["r"], 1))
    return out


def favourites(path=None, reader_id=READER_ID) -> list[dict]:
    """On Deck's Favourites row: starred series (as series tiles) then starred
    books (as book tiles), newest star first."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa.ondeck import _card
    out = []
    with db._connect(path) as conn:
        for m in conn.execute("""SELECT kind, target_id, updated_at FROM marks WHERE reader_id = ? AND favourite = 1
                                 ORDER BY kind DESC, updated_at DESC""",            # series first, then books
                              (reader_id,)):
            if m["kind"] == "series":
                s = db.get_series_by_id(m["target_id"], path)
                if s:
                    out.append({"kind": "series", "series_id": s["id"], "series": s["title"], "label": s["title"]})
            else:
                b = conn.execute("SELECT * FROM books WHERE id = ?", (m["target_id"],)).fetchone()
                if b and os.path.exists(b["path"]):
                    p = conn.execute("SELECT page, completed, updated_at FROM read_progress WHERE reader_id = ? AND book_id = ?",
                                     (reader_id, b["id"])).fetchone()
                    out.append(dict(_card(conn, b, dict(p) if p else None), kind="book"))
    return out


# --- next in series: what the end screen counts down to ------------------------------
def next_in_series(book_id: int, path=None, reader_id=READER_ID) -> dict:
    """The book after this one in its folder, or why there isn't one:
    {status: 'book', book_id, title, label} | {status: 'upcoming'|'not_owned'|'downloading'|'queued'|'end', label}."""
    path = path or DB_PATH
    b = db.get_book(book_id, path)
    if not b:
        raise HTTPException(404, "No such book")
    with db._connect(path) as conn:
        title = None
        if b.get("tracked_series_id"):
            r = conn.execute("SELECT title FROM tracked_series WHERE id = ?", (b["tracked_series_id"],)).fetchone()
            title = r["title"] if r else None
        if not title and b.get("shelf_series_id"):
            r = conn.execute("SELECT title FROM shelf_series WHERE id = ?", (b["shelf_series_id"],)).fetchone()
            title = r["title"] if r else None
        title = title or os.path.basename(os.path.dirname(b["path"]))
        if b.get("number") is not None and b.get("shelf_series_id"):
            nxt = conn.execute("""SELECT id, number, path FROM books WHERE shelf_series_id = ? AND number > ?
                ORDER BY number LIMIT 1""", (b["shelf_series_id"], b["number"])).fetchone()
            if nxt and os.path.exists(nxt["path"]):
                return {"status": "book", "book_id": nxt["id"], "title": title, "number": nxt["number"], "label": f"#{nxt['number']:g}"}
        if b.get("number") is not None and b.get("tracked_series_id"):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            i = conn.execute("""SELECT number, store_date FROM issue_status WHERE tracked_series_id = ? AND owned = 0
                AND ignored = 0 AND number > ? ORDER BY number LIMIT 1""", (b["tracked_series_id"], b["number"])).fetchone()
            if i:
                q = conn.execute("""SELECT state FROM download_queue WHERE tracked_series_id = ? AND issue_number = ?
                    AND state NOT IN ('done','failed','not_found','cancelled')""", (b["tracked_series_id"], i["number"])).fetchone()
                state = q["state"] if q else None
                lab = f"#{i['number']:g}"
                if state in ("downloading", "pending_usenet", "pending_torrent", "processing", "searching"):
                    return {"status": "downloading", "label": f"{lab} is downloading", "number": i["number"]}
                if i["store_date"] and i["store_date"] > today:
                    d = datetime.strptime(i["store_date"], "%Y-%m-%d")
                    when = d.strftime("%a") if d - datetime.now() < timedelta(days=6) else d.strftime("%-d %b")
                    return {"status": "upcoming", "label": f"{lab} out {when}", "number": i["number"], "store_date": i["store_date"]}
                if state in ("queued", "waiting"):
                    return {"status": "queued", "label": f"{lab} is in the queue", "number": i["number"]}
                return {"status": "not_owned", "label": f"{lab} isn't on the shelf", "number": i["number"]}
    return {"status": "end", "label": "That's the last one here"}


# --- API --------------------------------------------------------------------------
class MarkRequest(BaseModel):
    favourite: bool | None = None
    rating: int | None = None
    clear_rating: bool = False


def _set(kind: str, target_id: int, req: MarkRequest) -> dict:
    try:
        return set_mark(kind, target_id, req.favourite, req.rating, req.clear_rating)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.put("/api/books/{book_id}/mark")
def api_mark_book(book_id: int, req: MarkRequest):
    if not db.get_book(book_id, DB_PATH):
        raise HTTPException(404, "No such book")
    return _set("book", book_id, req)


@router.put("/api/series/{series_id}/mark")
def api_mark_series(series_id: int, req: MarkRequest):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404, "No such series")
    out = _set("series", series_id, req)
    return {**out, **{k: v for k, v in series_rating(series_id).items() if k in ("rating_derived", "rated_issues")}}


@router.get("/api/books/{book_id}/next")
def api_next(book_id: int):
    return next_in_series(book_id)


@router.get("/api/ondeck/favourites")
def api_favourites():
    return {"favourites": favourites()}
