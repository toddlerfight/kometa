"""On Deck — where reading happens (docs/reader-spec.md).

Three rows, each straight from the reading tables:
  continue  — books with partial progress, most recently read first; a book
              untouched for 30 days drops off (its progress is kept).
  next      — series you're reading (at least one finished, nothing in
              progress): the first unread owned book AFTER the last one you
              finished, in Kometa's own issue order. Trades and oddities
              (no number) never count as 'next'.
  soon      — series you're reading and caught up on whose next issue isn't
              here yet: not released, released but not downloaded, or
              downloading. Labelled from Kometa's real data.
Suggestions and reading lists come later.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

import kometa.db as db
from kometa.reader import READER_ID

router = APIRouter()
DB_PATH = db.DB_PATH
CONTINUE_DAYS = 30
ROW_LIMIT = 24


def _label(number, path_):
    return f"#{number:g}" if number is not None else os.path.splitext(os.path.basename(path_))[0]


def _series_name(conn, b) -> tuple[str, int | None, int | None]:
    """(title, tracked_series_id, shelf_series_id)"""
    if b["tracked_series_id"]:
        r = conn.execute("SELECT title FROM tracked_series WHERE id = ?", (b["tracked_series_id"],)).fetchone()
        if r:
            return r["title"], b["tracked_series_id"], b["shelf_series_id"]
    if b["shelf_series_id"]:
        r = conn.execute("SELECT title FROM shelf_series WHERE id = ?", (b["shelf_series_id"],)).fetchone()
        if r:
            return r["title"], None, b["shelf_series_id"]
    return os.path.basename(os.path.dirname(b["path"])), None, None


def _card(conn, b, progress=None, **extra) -> dict:
    title, tid, sid = _series_name(conn, b)
    return {"book_id": b["id"], "series": title, "series_id": tid, "shelf_id": sid,
            "label": _label(b["number"], b["path"]), "number": b["number"], "page_count": b["page_count"],
            "progress": progress, **extra}


def continue_reading(reader_id=READER_ID, now=None, path=None) -> list[dict]:
    path = path or DB_PATH
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=CONTINUE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.*, p.page, p.updated_at FROM read_progress p JOIN books b ON b.id = p.book_id
            WHERE p.reader_id = ? AND p.completed = 0 AND p.updated_at >= ?
            ORDER BY p.updated_at DESC LIMIT ?""", (reader_id, since, ROW_LIMIT)).fetchall()
        return [_card(conn, r, {"page": r["page"], "updated_at": r["updated_at"]}) for r in rows if os.path.exists(r["path"])]


def _series_groups(conn, reader_id):
    """Per series (tracked id or shelf id): books in issue order with progress."""
    rows = conn.execute("""
        SELECT b.*, p.completed, p.updated_at FROM books b
        LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
        ORDER BY b.number IS NULL, b.number, b.path""", (reader_id,)).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        key = ("t", r["tracked_series_id"]) if r["tracked_series_id"] else ("s", r["shelf_series_id"])
        if key[1] is None:
            continue
        groups.setdefault(key, []).append(r)
    return groups


def up_next(reader_id=READER_ID, path=None) -> list[dict]:
    path = path or DB_PATH
    out = []
    with db._connect(path) as conn:
        for key, books in _series_groups(conn, reader_id).items():
            numbered = [b for b in books if b["number"] is not None]
            if any(b["completed"] == 0 and b["updated_at"] for b in books):
                continue                                           # something in progress → Continue row
            done = [b for b in numbered if b["completed"] == 1]
            if not done:
                continue
            last_done = max(b["number"] for b in done)
            last_read_at = max(b["updated_at"] for b in done)
            nxt = next((b for b in numbered if b["number"] > last_done and not b["completed"] and os.path.exists(b["path"])), None)
            if nxt:
                out.append(_card(conn, nxt, None, last_read_at=last_read_at, finished=len(done)))
    out.sort(key=lambda c: c["last_read_at"], reverse=True)
    return out[:ROW_LIMIT]


def coming_soon(reader_id=READER_ID, path=None) -> list[dict]:
    """Series you're reading and caught up on (every numbered owned book
    finished) whose issue list has something you don't own yet."""
    path = path or DB_PATH
    out = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db._connect(path) as conn:
        queue = {(q["tracked_series_id"], q["issue_number"]): q["state"] for q in conn.execute(
            "SELECT tracked_series_id, issue_number, state FROM download_queue WHERE state NOT IN ('done','failed','not_found','cancelled')")}
        for key, books in _series_groups(conn, reader_id).items():
            if key[0] != "t":
                continue
            tid = key[1]
            numbered = [b for b in books if b["number"] is not None]
            if not numbered or any(b["completed"] == 0 and b["updated_at"] for b in books):
                continue
            done = [b for b in numbered if b["completed"] == 1]
            if not done or len(done) < len(numbered):
                continue
            last_done = max(b["number"] for b in done)
            nxt = conn.execute("""
                SELECT number, store_date FROM issue_status
                WHERE tracked_series_id = ? AND owned = 0 AND ignored = 0 AND number > ?
                ORDER BY number LIMIT 1""", (tid, last_done)).fetchone()
            if not nxt:
                continue
            s = conn.execute("SELECT title, on_pull_list FROM tracked_series WHERE id = ?", (tid,)).fetchone()
            state = queue.get((tid, nxt["number"]))
            if state in ("downloading", "pending_usenet", "pending_torrent", "processing", "searching"):
                status, label = "downloading", "Downloading"
            elif nxt["store_date"] and nxt["store_date"] > today:
                status, label = "upcoming", f"Out {nxt['store_date']}"
            elif state in ("queued", "waiting"):
                status, label = "queued", "In the queue"
            else:
                status, label = "not_owned", "Not owned" + ("" if s["on_pull_list"] else " · pull list off")
            out.append({"series": s["title"], "series_id": tid, "number": nxt["number"], "label": f"#{nxt['number']:g}",
                        "store_date": nxt["store_date"], "status": status, "status_label": label,
                        "last_read_at": max(b["updated_at"] for b in done), "on_pull_list": bool(s["on_pull_list"])})
    out.sort(key=lambda c: c["last_read_at"], reverse=True)
    return out[:ROW_LIMIT]


def _one_per_series(conn, rows, **field) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        key = ("t", r["tracked_series_id"]) if r["tracked_series_id"] else ("s", r["shelf_series_id"])
        if key[1] is None or key in seen or not os.path.exists(r["path"]):
            continue
        seen.add(key)
        out.append(_card(conn, r, None, **{k: r[v] for k, v in field.items()}))
        if len(out) >= ROW_LIMIT:
            break
    return out


def recent_added(reader_id=READER_ID, path=None) -> list[dict]:
    """What landed on the shelf lately, by the FILE's own timestamp (the index
    date lies: the whole shelf was indexed this week). One card per series,
    its newest file, unread only — seven Deadly Duo issues in a night is one card."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.*, p.completed FROM books b
            LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE COALESCE(p.completed, 0) = 0
            ORDER BY b.mtime DESC, b.number DESC LIMIT 400""", (reader_id,)).fetchall()
        return _one_per_series(conn, rows, mtime="mtime")


def recent_released(reader_id=READER_ID, path=None) -> list[dict]:
    """Owned issues by RELEASE date (the catalogue's store date), newest first,
    unread only, one card per series. What came out lately that you have."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.*, i.store_date, p.completed FROM issue_status i
            JOIN books b ON b.tracked_series_id = i.tracked_series_id AND b.number = i.number
            LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE i.owned = 1 AND i.store_date IS NOT NULL AND COALESCE(p.completed, 0) = 0
            ORDER BY i.store_date DESC, b.number DESC LIMIT 400""", (reader_id,)).fetchall()
        return _one_per_series(conn, rows, store_date="store_date")


@router.get("/api/ondeck")
def on_deck():
    return {"continue": continue_reading(), "next": up_next(), "soon": coming_soon(),
            "released": recent_released(), "added": recent_added()}
