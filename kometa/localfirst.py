"""Read local first (docs/library-enrichment-spec.md, step 5) — the issue sheet's
details from what we already hold, the catalogues only when that's nothing.

Behind ONE switch, config read_local_first (absent = '0'): off, record.details()
runs exactly as before. On, an issue's details are read in this order:

  1. the DB record, when it holds catalogue data (metron / locg / gcd, or our
     own ComicInfo read back) — answered there, no network at all;
  2. the file's own ComicInfo, IF Kometa wrote it (the Notes marker) — parsed
     back into the record as source 'comicinfo-kometa' and answered;
  3. online (today's resolver) — written to the record as today, the book queued
     for a retag (deferred while it's open in the reader), and the fallback
     logged as a pipeline miss: an online fetch at view time means a trickle,
     a sweep or the retag run missed this issue, and the list says which class.

A foreign uploader's ComicInfo is never step 2: it stays the stopgap it was.
"""
import logging
import threading
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

OPEN_WINDOW_S = 20 * 60         # a book is 'open' if a page was served, or progress written, this recently
MISSES_LIST_LIMIT = 200


def enabled(path=None) -> bool:
    return db.get_config(path or DB_PATH).get("read_local_first", "0") == "1"


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        # every view-time online fallback (5c): what the record and the file lacked
        conn.execute("""CREATE TABLE IF NOT EXISTS localfirst_miss (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracked_series_id INTEGER NOT NULL, number REAL NOT NULL, book_id INTEGER,
            record_state TEXT, file_state TEXT, reason TEXT NOT NULL, outcome TEXT,
            at TEXT NOT NULL DEFAULT (datetime('now')))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_localfirst_miss_at ON localfirst_miss(at)")
        # retags waiting for their book to be closed (5b): a file is never swapped
        # under the reader
        conn.execute("""CREATE TABLE IF NOT EXISTS retag_deferred (
            book_id INTEGER PRIMARY KEY, queued_for TEXT NOT NULL,
            deferred_at TEXT NOT NULL DEFAULT (datetime('now')))""")


# --- is the book open? -----------------------------------------------------------------
def _progress_age_s(book_id: int, path) -> float | None:
    from kometa.reader import READER_ID
    with db._connect(path) as conn:
        r = conn.execute("SELECT updated_at FROM read_progress WHERE reader_id = ? AND book_id = ?",
                         (READER_ID, book_id)).fetchone()
    if not r or not r["updated_at"]:
        return None
    try:
        at = datetime.strptime(r["updated_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - at).total_seconds()


def is_open(book_id: int, path=None, now: float | None = None) -> bool:
    """Open = a page of it was served (web reader or OPDS, this process) or its
    progress was written (any device, client clock) in the last OPEN_WINDOW_S.
    A reader with every page cached still writes progress as it turns them."""
    import time
    from kometa.activity import last_page_served
    path = path or DB_PATH
    if (now or time.time()) - last_page_served(book_id) < OPEN_WINDOW_S:
        return True
    age = _progress_age_s(book_id, path)
    return age is not None and -300 < age < OPEN_WINDOW_S      # a few minutes of client clock skew forgiven


# --- the retag, queued or deferred (5b) ---------------------------------------------------
def queue_retag(book_ids, queued_for: str = "changed", path=None) -> dict:
    """retag.enqueue for each closed book; an open one waits in retag_deferred
    until flush_deferred finds it closed. retag's own switch still decides
    whether a file is ever written — this only puts rows on its list."""
    from kometa import retag
    path = path or DB_PATH
    ensure_tables(path)
    queued, deferred = 0, 0
    for bid in book_ids:
        if is_open(bid, path):
            with db._connect(path) as conn:
                conn.execute("INSERT INTO retag_deferred (book_id, queued_for) VALUES (?, ?) "
                             "ON CONFLICT(book_id) DO NOTHING", (bid, queued_for))
            deferred += 1
        else:
            queued += retag.enqueue([bid], queued_for, path)
    return {"queued": queued, "deferred": deferred}


def flush_deferred(path=None) -> int:
    """Scheduler tick: every deferred book that's been closed goes on retag's list."""
    from kometa import retag
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT book_id, queued_for FROM retag_deferred ORDER BY deferred_at")]
    n = 0
    for r in rows:
        if is_open(r["book_id"], path):
            continue
        n += retag.enqueue([r["book_id"]], r["queued_for"], path)
        with db._connect(path) as conn:
            conn.execute("DELETE FROM retag_deferred WHERE book_id = ?", (r["book_id"],))
    return n


def deferred(path=None) -> list[int]:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT book_id FROM retag_deferred ORDER BY deferred_at")]


# --- the misses (5c) ----------------------------------------------------------------------
def log_miss(series_id: int, number: float, record_state: str, file_state: str, outcome: str | None,
             book_id: int | None = None, path=None) -> None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("""INSERT INTO localfirst_miss (tracked_series_id, number, book_id, record_state, file_state, reason, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (series_id, number, book_id, record_state, file_state,
                                               f"{record_state} / {file_state}", outcome))


def miss_count(path=None) -> int:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM localfirst_miss").fetchone()[0]


def misses(path=None, limit: int = MISSES_LIST_LIMIT) -> dict:
    """Latest first, plus counts by reason (record state / file state) so a
    whole class gets fixed, not one issue at a time."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        by = {r["reason"]: r["n"] for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM localfirst_miss GROUP BY reason ORDER BY n DESC, reason")}
        rows = [dict(r) for r in conn.execute("""SELECT m.*, s.title AS series_title FROM localfirst_miss m
            LEFT JOIN tracked_series s ON s.id = m.tracked_series_id ORDER BY m.id DESC LIMIT ?""", (limit,))]
    return {"enabled": enabled(path), "total": sum(by.values()), "by_reason": by, "misses": rows}


# --- the reading order (5a) ----------------------------------------------------------------
def _answer(row: dict, from_record: bool, issue: dict) -> dict:
    return {"desc": row.get("desc") or "", "credits": row.get("credits") or [], "source": row.get("source"),
            "arcs": row.get("arcs") or [], "store_date": row.get("store_date") or issue.get("store_date"),
            "record": from_record}


def _record_state(row: dict | None) -> str:
    from kometa import record
    if not row:
        return "no record"
    if row.get("fill_state") == "miss":
        return "record miss"
    if row.get("fill_state") == "variants":
        return "covers only"
    if row.get("source") in record.STOPGAP_SOURCES:
        return f"stopgap ({row['source']})"
    return f"record ({row.get('source') or 'unknown'})"


def _books(series_id: int, number: float, path) -> list[dict]:
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, path FROM books WHERE tracked_series_id = ? AND number = ? ORDER BY mtime DESC, id",
            (series_id, number))]


def _from_file(issue: dict, books: list[dict], path) -> tuple[dict | None, str]:
    """Step 2: the first book whose ComicInfo Kometa wrote, into the record.
    Returns (row, file_state); file_state says why not when row is None."""
    import os
    from kometa import comicinfo, covers, record
    if not books:
        return None, "no book on the shelf"
    state = "file missing"
    for b in books:
        if not os.path.exists(b["path"]):
            continue
        ci = covers.read_comicinfo(b["path"])
        if not ci:
            state = "no ComicInfo" if state in ("file missing", "no ComicInfo") else state
            continue
        parsed = comicinfo.parse_ours(ci)
        if parsed is None:
            state = "foreign ComicInfo"
            continue
        # the ids the file carries fill any the issue row lacks — never replace them
        ids = {k: issue.get(k) or parsed.get(k) for k in ("metron_issue_id", "locg_issue_id", "cv_issue_id")}
        row = record.write_issue(issue["tracked_series_id"], float(issue["number"]), parsed, comicinfo.OURS_SOURCE,
                                 "full", {**issue, **ids}, path)
        return row, "ours"
    return None, state


def _spawn(fn) -> None:
    """The stopgap refresh, behind the answer (tests run it inline)."""
    threading.Thread(target=fn, daemon=True).start()


def details(issue: dict, path=None) -> dict:
    """record.details() with read_local_first on. Same response shape."""
    from kometa import issue_meta, record
    path = path or DB_PATH
    sid, number = issue["tracked_series_id"], float(issue["number"])
    row = record.get_issue(sid, number, path)
    # 1. the record, when a catalogue (or our own tags) filled it: no network, not even behind
    if row and row.get("fill_state") in ("full", "partial") and row.get("source") in record.CATALOGUE_SOURCES:
        if not record._fresh(row) and (issue.get("metron_issue_id") or issue.get("locg_issue_id")):
            # past the record TTL: answer now, refresh behind (as the flag-off path does);
            # a catalogue that answered rewrites the file's tags too
            ids = [b["id"] for b in _books(sid, number, path)]

            def refresh():
                try:
                    filled = record.fill_issue(sid, number, path, force=True)
                    if filled and filled.get("source") in record.CATALOGUE_SOURCES and ids:
                        queue_retag(ids, "changed", path)
                except Exception as e:
                    logger.info(f"Read-local refresh for {sid}#{number:g} skipped: {e}")
            _spawn(refresh)
        return _answer(row, True, issue)
    # 2. the file's own ComicInfo, when it's ours
    books = _books(sid, number, path)
    got, file_state = _from_file(issue, books, path)
    if got:
        return _answer(got, True, issue)
    # 3. online — a pipeline miss either way
    record_state = _record_state(row)
    book_id = books[0]["id"] if books else None
    ids = [b["id"] for b in books]
    if row and row.get("fill_state") in ("full", "partial"):
        # a stopgap (the uploader's tags, Komga): shown now, a catalogue asked behind, as today
        def refresh():
            outcome = None
            try:
                filled = record.fill_issue(sid, number, path, force=True)
                outcome = filled.get("source") if filled and filled.get("source") in record.CATALOGUE_SOURCES else "nothing"
                if outcome != "nothing" and ids:
                    queue_retag(ids, "changed", path)
            except Exception as e:
                outcome = "error"
                logger.info(f"Read-local refresh for {sid}#{number:g} skipped: {e}")
            finally:
                log_miss(sid, number, record_state, file_state, outcome, book_id, path)
        _spawn(refresh)
        return _answer(row, True, issue)
    if not (issue.get("metron_issue_id") or issue.get("locg_issue_id")):
        # no catalogue id to ask with: nothing online can answer — say so, ask nothing
        log_miss(sid, number, record_state, file_state, "no catalogue id", book_id, path)
        return {"desc": "", "credits": [], "source": None, "arcs": [], "store_date": issue.get("store_date"), "record": False}
    try:
        d = issue_meta.details_for(issue, path)
    except Exception:
        log_miss(sid, number, record_state, file_state, "error", book_id, path)
        raise
    if d.get("source"):
        state = "full" if d["source"].startswith("metron") else "partial"
        record.write_issue(sid, number, d, d["source"].split("+")[0], state, issue, path)
        if ids:
            queue_retag(ids, "changed", path)
    log_miss(sid, number, record_state, file_state, d.get("source") or "nothing", book_id, path)
    return {"desc": d.get("desc", ""), "credits": d.get("credits", []), "source": d.get("source"),
            "arcs": d.get("arcs") or [], "store_date": d.get("store_date") or issue.get("store_date"), "record": False}
