"""Want to read — a list you build by hand, shown as a row on On Deck.

Three kinds of entry, all in one table:
  book    — a file on the shelf (a trade, a one-shot, an untracked book): book_id
  issue   — an issue of a tracked run, owned or not: series_id + number. Kept by
            issue, not by file, so a rename or a re-download doesn't lose it, and
            one you don't own yet becomes a book card the moment it lands
  series  — a whole run: series_id, no number. The card is the next unread issue
            you own; nothing owned to read → the series itself, 'not here'

An entry leaves when you've finished it: a book or issue once it's read, a series
once every issue you own is read and nothing is missing. Starting one puts it in
Continue reading as well; it stays here until it's done. The row keeps the order
you added things in, oldest first — a queue, not a feed.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db
from kometa.reader import READER_ID

router = APIRouter()
DB_PATH = db.DB_PATH


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS want_to_read (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reader_id TEXT NOT NULL,
            book_id INTEGER, series_id INTEGER, number REAL,
            added_at TEXT NOT NULL)""")
        # one entry per thing: COALESCE because SQLite counts NULLs as distinct
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS want_to_read_one ON want_to_read
            (reader_id, COALESCE(book_id, -1), COALESCE(series_id, -1), COALESCE(number, -1))""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target(book_id=None, series_id=None, number=None, path=None) -> tuple:
    """Normalise what was tapped: a book that IS an issue of a tracked run is kept
    as that issue (series_id, number), so the entry outlives the file."""
    path = path or DB_PATH
    if book_id is not None:
        b = db.get_book(book_id, path)
        if not b:
            raise HTTPException(404, "No such book")
        if b.get("tracked_series_id") and b.get("number") is not None:
            return None, b["tracked_series_id"], float(b["number"])
        return int(book_id), None, None
    if series_id is None:
        raise HTTPException(400, "Say what to add: a book, an issue or a series")
    if not db.get_series_by_id(series_id, path):
        raise HTTPException(404, "No such series")
    return None, int(series_id), (float(number) if number is not None else None)


def items(path=None, reader_id=READER_ID) -> list[dict]:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, book_id, series_id, number, added_at FROM want_to_read WHERE reader_id = ? ORDER BY added_at, id",
            (reader_id,))]


def find(book_id=None, series_id=None, number=None, path=None, reader_id=READER_ID) -> dict | None:
    path = path or DB_PATH
    t = _target(book_id, series_id, number, path)
    return next((i for i in items(path, reader_id) if (i["book_id"], i["series_id"], i["number"]) == t), None)


def add(book_id=None, series_id=None, number=None, path=None, reader_id=READER_ID) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    bid, sid, n = _target(book_id, series_id, number, path)
    with db._connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO want_to_read (reader_id, book_id, series_id, number, added_at) VALUES (?, ?, ?, ?, ?)",
                     (reader_id, bid, sid, n, _now()))
    return find(book_id, series_id, number, path, reader_id)


def remove(want_id: int, path=None, reader_id=READER_ID) -> None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("DELETE FROM want_to_read WHERE id = ? AND reader_id = ?", (want_id, reader_id))


def row(path=None, reader_id=READER_ID) -> list[dict]:
    """On Deck's Want to read cards, in the order you added them. Finished
    entries are dropped here, as the row is built."""
    from kometa.ondeck import _card
    path = path or DB_PATH
    ensure_tables(path)
    cards, done = [], []
    with db._connect(path) as conn:
        def progress(bid):
            p = conn.execute("SELECT page, completed, updated_at FROM read_progress WHERE reader_id = ? AND book_id = ?",
                             (reader_id, bid)).fetchone()
            return dict(p) if p else None

        def book_card(b, w):
            p = progress(b["id"])
            if p and p["completed"]:
                return None
            c = _card(conn, b, p if p and p["page"] else None, kind="book", want_id=w["id"])
            return c

        for w in conn.execute("SELECT * FROM want_to_read WHERE reader_id = ? ORDER BY added_at, id", (reader_id,)).fetchall():
            if w["book_id"] is not None:                                   # a file
                b = conn.execute("SELECT * FROM books WHERE id = ?", (w["book_id"],)).fetchone()
                if not b:
                    continue                                               # gone from the shelf: keep the entry, show nothing
                c = book_card(b, w)
                (cards.append(c) if c else done.append(w["id"]))
                continue
            s = conn.execute("SELECT id, title FROM tracked_series WHERE id = ?", (w["series_id"],)).fetchone()
            if not s:
                done.append(w["id"])                                       # the run was removed
                continue
            if w["number"] is not None:                                    # an issue
                b = conn.execute("SELECT * FROM books WHERE tracked_series_id = ? AND number = ? ORDER BY id LIMIT 1",
                                 (s["id"], w["number"])).fetchone()
                if b:
                    c = book_card(b, w)
                    (cards.append(c) if c else done.append(w["id"]))
                else:
                    cards.append({"kind": "gap", "series": s["title"], "series_id": s["id"], "number": w["number"],
                                  "label": f"#{w['number']:g}", "cover": f"/api/series/{s['id']}/issues/{w['number']:g}/thumbnail",
                                  "want_id": w["id"]})
                continue
            # a whole run: the first unread issue you own after the last one you finished
            books = conn.execute("""SELECT b.* FROM books b WHERE b.tracked_series_id = ? AND b.number IS NOT NULL
                                    ORDER BY b.number""", (s["id"],)).fetchall()
            read = {b["id"] for b in books if (progress(b["id"]) or {}).get("completed")}
            last = max((b["number"] for b in books if b["id"] in read), default=None)
            nxt = next((b for b in books if b["id"] not in read and (last is None or b["number"] > last)), None) \
                or next((b for b in books if b["id"] not in read), None)
            if nxt:
                c = book_card(nxt, w)
                c["want_series"] = True
                cards.append(c)
                continue
            missing = conn.execute("""SELECT COUNT(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 0
                                      AND ignored = 0 AND covered_by IS NULL""", (s["id"],)).fetchone()[0]
            if missing:
                cards.append({"kind": "series", "series": s["title"], "series_id": s["id"], "label": s["title"],
                              "missing": missing, "want_id": w["id"], "want_series": True})
            else:
                done.append(w["id"])                                       # all read, nothing to come
        if done:
            conn.executemany("DELETE FROM want_to_read WHERE id = ?", [(i,) for i in done])
    return cards


class WantRequest(BaseModel):
    book_id: int | None = None
    series_id: int | None = None
    number: float | None = None


@router.get("/api/wantread")
def api_items():
    """Everything on the list (ids only) — enough for a menu to say Add or Remove."""
    return items()


@router.get("/api/wantread/check")
def api_check(book_id: int | None = None, series_id: int | None = None, number: float | None = None):
    w = find(book_id, series_id, number)
    return {"want_id": w["id"] if w else None}


@router.post("/api/wantread")
def api_add(req: WantRequest):
    w = add(req.book_id, req.series_id, req.number)
    return {"want_id": w["id"] if w else None}


@router.delete("/api/wantread/{want_id}", status_code=204)
def api_remove(want_id: int):
    remove(want_id)
