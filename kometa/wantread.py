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

Wanting something that isn't here goes and GETS it (2026-10-24 — Want to read doubles
as the request; the apps have no Track or Download). A run Kometa doesn't track yet —
a recommendation, known only by its catalogue id — is tracked first. One issue is that
issue and nothing more; a whole run follows Track's rule (main._auto_pull): 33 or fewer
missing → pull list on, all of them; more → pull list off, the first 33. Not out yet →
the nightly sweep takes it on release day (db.get_missing_for_monitored). Taken off the
list before it came → what's still only waiting in the queue goes too.
"""
import logging
import threading
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db
from kometa.auth import current_reader
logger = logging.getLogger(__name__)
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
        # the queue rows a want put there — the ONLY ones taking it off may cancel. One you
        # queued by hand on the same run is yours, whatever happens to the list
        conn.execute("""CREATE TABLE IF NOT EXISTS want_queued (
            series_id INTEGER NOT NULL, number REAL NOT NULL, PRIMARY KEY (series_id, number))""")


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


def items(path=None, reader_id=None) -> list[dict]:
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, book_id, series_id, number, added_at FROM want_to_read WHERE reader_id = ? ORDER BY added_at, id",
            (reader_id,))]


def find(book_id=None, series_id=None, number=None, path=None, reader_id=None) -> dict | None:
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    t = _target(book_id, series_id, number, path)
    return next((i for i in items(path, reader_id) if (i["book_id"], i["series_id"], i["number"]) == t), None)


def add(book_id=None, series_id=None, number=None, path=None, reader_id=None) -> dict:
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    ensure_tables(path)
    bid, sid, n = _target(book_id, series_id, number, path)
    with db._connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO want_to_read (reader_id, book_id, series_id, number, added_at) VALUES (?, ?, ?, ?, ?)",
                     (reader_id, bid, sid, n, _now()))
    return find(book_id, series_id, number, path, reader_id)


def remove(want_id: int, path=None, reader_id=None) -> None:
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        w = conn.execute("SELECT * FROM want_to_read WHERE id = ? AND reader_id = ?", (want_id, reader_id)).fetchone()
        conn.execute("DELETE FROM want_to_read WHERE id = ? AND reader_id = ?", (want_id, reader_id))
    if w and w["series_id"] is not None:
        cancel(w["series_id"], w["number"], path)


# --- the request: wanting what isn't here gets it ------------------------------------------
def run_for(metron_series_id=None, locg_series_id=None, title=None, year=None, publisher=None,
            path=None, root=None) -> tuple[dict, bool]:
    """The tracked series for a catalogue run → (series, created). A run Kometa doesn't track
    yet is tracked here, pull list OFF; fetch() decides what comes."""
    from kometa import listget, sources
    path = path or DB_PATH
    run = {"source": "metron", "id": int(metron_series_id)} if metron_series_id else {"source": "locg", "id": int(locg_series_id)}
    existing = listget._existing_for(run, path)
    if existing:
        return existing, False
    if not title:
        raise HTTPException(400, "A run Kometa doesn't track needs its title")
    if metron_series_id and not publisher:
        try:                                     # a recommendation carries no publisher: the folder needs one
            from kometa import metron_client
            publisher = metron_client.series_detail(int(metron_series_id)).get("publisher")
        except Exception as e:
            logger.info(f"Want: Metron publisher lookup for {title!r} skipped: {e}")
    run.update(title=title, year=year, publisher=publisher)
    s = listget._track(run, None, path, root or sources.comics_root())
    logger.info(f"Want: tracked {s['title']!r} (#{s['id']}) to fetch it")
    return s, True


def fetch(series_id: int, number=None, created=False, path=None, sync=None, process=None, today=None) -> int:
    """Queue what a Want to read entry asks for. Returns how many were queued."""
    from kometa import listget, main
    path = path or DB_PATH
    ensure_tables(path)
    s = db.get_series_by_id(series_id, path)
    if not s:
        return 0
    if created:
        (sync or listget._sync)(s)
    issues = db.get_issues_for_series(series_id, path)
    today = today or str(date.today())
    before = _waiting(series_id, path)
    if number is None:
        if s["on_pull_list"] and not created:
            return 0                             # a run already followed: the sweep has it in hand
        queued = main._auto_pull(series_id, issues, today, path)
    else:
        n = float(number)
        if created:                              # one issue asked for: 1 of 1, not 1 of 60
            for i in issues:
                if i["number"] != n and not i["owned"]:
                    db.set_issue_ignored(series_id, i["number"], True, path)
        it = next((i for i in issues if i["number"] == n), None)
        queued = 0
        if it and not it["owned"]:
            if it.get("ignored"):
                db.set_issue_ignored(series_id, n, False, path)
            if not it["store_date"] or it["store_date"] <= today:
                db.queue_issue(series_id, n, path)
                queued = 1
    if queued:
        ours = _waiting(series_id, path) - before
        if ours:
            with db._connect(path) as conn:
                conn.executemany("INSERT OR IGNORE INTO want_queued (series_id, number) VALUES (?, ?)",
                                 [(series_id, n) for n in ours])
        (process or (lambda: threading.Thread(target=main._process_queue, daemon=True).start()))()
    logger.info(f"Want: {s['title']!r}{'' if number is None else f' #{float(number):g}'} → {queued} queued")
    return queued


def _waiting(series_id: int, path) -> set[float]:
    with db._connect(path) as conn:
        return {r[0] for r in conn.execute("""SELECT issue_number FROM download_queue WHERE tracked_series_id = ?
            AND state IN ('queued', 'not_found', 'failed')""", (series_id,))}


def cancel(series_id: int, number=None, path=None) -> int:
    """Taken off the list before it came: drop what's still only WAITING in the queue for it —
    only rows a want put there (want_queued), never one queued by hand. Not on a run that's on
    the pull list (followed either way), and nothing someone else still wants. In-flight
    searches and downloads finish. Returns how many rows went."""
    path = path or DB_PATH
    ensure_tables(path)
    s = db.get_series_by_id(series_id, path)
    if not s or s["on_pull_list"]:
        return 0
    with db._connect(path) as conn:
        others = [r["number"] for r in conn.execute("SELECT number FROM want_to_read WHERE series_id = ?", (series_id,))]
        if any(n is None for n in others):
            return 0                             # someone wants the whole run
        ours = {r[0] for r in conn.execute("SELECT number FROM want_queued WHERE series_id = ?", (series_id,))}
    nums = [n for n in ([float(number)] if number is not None else ours) if n in ours and n not in others]
    gone = sum(db.dequeue_waiting_issue(series_id, n, path) for n in nums)
    with db._connect(path) as conn:
        conn.executemany("DELETE FROM want_queued WHERE series_id = ? AND number = ?", [(series_id, n) for n in nums])
    return gone


def _fetch_quietly(series_id, number, created):
    try:
        fetch(series_id, number, created)
    except Exception:
        logger.exception(f"Want: fetching series {series_id} #{number} failed")


def row(path=None, reader_id=None) -> list[dict]:
    """On Deck's Want to read cards, in the order you added them. Finished
    entries are dropped here, as the row is built."""
    reader_id = reader_id or current_reader()
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


# --- Want to read as a reading list (2026-10-26) --------------------------------------
# The same pile, pinned first on Lists and in the OPDS feed (Panels never saw it): one
# list per reader, built from row() — the same entries, the same order, the same rule
# that a finished one leaves. Reserved id: no table row, so nothing to sync by hand.
WANT_LIST_ID = -1
WANT_LIST_NAME = "Want to read"


def _num_text(n) -> str | None:
    return None if n is None else (f"{int(n)}" if float(n) == int(n) else f"{n:g}")


def as_list(path=None, reader_id=None) -> dict:
    """The reader's Want to read in a resolved list's shape (readlists.resolve)."""
    entries = []
    for i, c in enumerate(row(path, reader_id), 1):
        e = {"item_id": c["want_id"], "position": i, "series": c["series"], "series_id": c.get("series_id"),
             "number": _num_text(c.get("number")), "status": "missing", "cover": None, "books": []}
        if c.get("book_id"):
            p = c.get("progress")
            e["status"] = "owned"
            e["books"] = [{"id": c["book_id"], "number": c.get("number"), "label": c["label"], "page_count": c.get("page_count"),
                           "progress": {"page": p["page"], "completed": False, "updated_at": p.get("updated_at")} if p else None}]
        elif c["kind"] == "gap":                                           # an issue not here yet
            e["cover"] = c["cover"]
        else:                                                              # a whole run, nothing of it to read yet
            e["cover"] = f"/api/series/{c['series_id']}/thumbnail"
        entries.append(e)
    books = [b for e in entries for b in e["books"]]
    first = next((b["id"] for b in books), None)
    return {"id": WANT_LIST_ID, "name": WANT_LIST_NAME, "source": "want", "source_ref": None,
            "mine": True, "shared": False, "can_share": False, "pinned": True,
            "entries": entries, "total": len(entries), "owned": sum(1 for e in entries if e["status"] == "owned"),
            "read": 0, "books": len(books), "books_read": 0,
            "books_reading": sum(1 for b in books if b["progress"]), "continue": first}


def list_card(path=None, reader_id=None) -> dict | None:
    """Want to read's card on the Lists page — none while it's empty."""
    res = as_list(path, reader_id)
    if not res["entries"]:
        return None
    first = next((b for e in res["entries"] for b in e["books"]), None)
    gap = next((e["cover"] for e in res["entries"] if e["cover"]), None)
    return {k: res[k] for k in ("id", "name", "source", "source_ref", "mine", "shared", "can_share", "pinned",
                                "total", "owned", "read", "books", "books_read", "books_reading")} | {
        "entries": res["total"], "cover_book_id": first["id"] if first else None, "cover": None if first else gap}


class WantRequest(BaseModel):
    book_id: int | None = None
    series_id: int | None = None
    number: float | None = None
    # a run Kometa doesn't track (a recommendation): its catalogue id, and what the card knows
    metron_series_id: int | None = None
    locg_series_id: int | None = None
    title: str | None = None
    year: int | None = None
    publisher: str | None = None


@router.get("/api/wantread")
def api_items():
    """Everything on the list (ids only) — enough for a menu to say Add or Remove."""
    return items()


@router.get("/api/wantread/check")
def api_check(book_id: int | None = None, series_id: int | None = None, number: float | None = None,
              metron_series_id: int | None = None, locg_series_id: int | None = None):
    if book_id is None and series_id is None and (metron_series_id or locg_series_id):
        from kometa import listget
        run = {"source": "metron", "id": metron_series_id} if metron_series_id else {"source": "locg", "id": locg_series_id}
        s = listget._existing_for(run, DB_PATH)
        if not s:
            return {"want_id": None, "series_id": None}  # never tracked, so nobody's wanted it
        series_id = s["id"]
    w = find(book_id, series_id, number)
    return {"want_id": w["id"] if w else None, "series_id": series_id}


@router.post("/api/wantread")
def api_add(req: WantRequest):
    sid, created = req.series_id, False
    if req.book_id is None and sid is None and (req.metron_series_id or req.locg_series_id):
        s, created = run_for(req.metron_series_id, req.locg_series_id, req.title, req.year, req.publisher)
        sid = s["id"]
    w = add(req.book_id, sid, req.number)
    if w and w["series_id"] is not None:          # a book is already here; an issue or a run may not be
        threading.Thread(target=_fetch_quietly, args=(w["series_id"], w["number"], created), daemon=True).start()
    return {"want_id": w["id"] if w else None, "series_id": w["series_id"] if w else sid}


@router.delete("/api/wantread/{want_id}", status_code=204)
def api_remove(want_id: int):
    remove(want_id)
