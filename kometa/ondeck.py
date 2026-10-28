"""On Deck — where reading happens (docs/reader-spec.md).

Three rows, each straight from the reading tables:
  continue  — books with partial progress, most recently read first; a book
              untouched for 30 days drops off (its progress is kept).
  next      — series you're reading: the first unread owned book AFTER the
              last one you finished (or are within a few pages of finishing —
              _through), in Kometa's own issue order, whatever else in the
              series is open. Off it only once you've started it (Continue
              has it then). Trades and oddities (no number) never count as 'next'.
  soon      — series you're reading and caught up on whose next issue isn't
              here yet: not released, released but not downloaded, or
              downloading. Labelled from Kometa's real data.
Discovery rows (docs/research/recommendation-patterns.md, rows 5, 7, 8):
  lists       — 'Next on {list}': reading lists you've read from lately, from
                the first unfinished entry on; gaps show as gaps. Max 2 rows.
  rediscover  — series you read and then left for 180+ days with unread books
                still on the shelf: the stalled book, else the next one.
  quick       — complete runs of ≤12 issues you've never opened.
A discovery row needs ≥4 cards to show, and a series shows in one row only
(task rows first, then the table order).
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

import kometa.db as db
from kometa.auth import current_reader
router = APIRouter()
DB_PATH = db.DB_PATH
CONTINUE_DAYS = 28          # Continue reading: books touched in the last four weeks (the owner, 2026-10-18; was 30)
ROW_LIMIT = 24
DISCOVERY_MIN = 4          # a discovery row with fewer cards isn't a row
LIST_RECENT_DAYS = 60      # a list counts as 'being read' if a book in it was read this recently
LIST_ROWS = 2
REDISCOVER_DAYS = 180
QUICK_MAX_ISSUES = 12


def _label(number, path_):
    return f"#{number:g}" if number is not None else os.path.splitext(os.path.basename(path_))[0]


def _series_name(conn, b) -> tuple[str, int | None, int | None]:
    """(title, tracked_series_id, shelf_series_id). The title rides on the row when
    the source query joined it in (tracked_title / shelf_title — the On Deck queries
    do, so no per-card SELECT); falls back to a lookup for any row that didn't."""
    keys = b.keys()
    if b["tracked_series_id"]:
        title = b["tracked_title"] if "tracked_title" in keys else None
        if title is None:
            r = conn.execute("SELECT title FROM tracked_series WHERE id = ?", (b["tracked_series_id"],)).fetchone()
            title = r["title"] if r else None
        if title:
            return title, b["tracked_series_id"], b["shelf_series_id"]
    if b["shelf_series_id"]:
        title = b["shelf_title"] if "shelf_title" in keys else None
        if title is None:
            r = conn.execute("SELECT title FROM shelf_series WHERE id = ?", (b["shelf_series_id"],)).fetchone()
            title = r["title"] if r else None
        if title:
            return title, None, b["shelf_series_id"]
    return os.path.basename(os.path.dirname(b["path"])), None, None


def _card(conn, b, progress=None, **extra) -> dict:
    title, tid, sid = _series_name(conn, b)
    return {"book_id": b["id"], "series": title, "series_id": tid, "shelf_id": sid,
            "label": _label(b["number"], b["path"]), "number": b["number"], "page_count": b["page_count"],
            "progress": progress, **extra}


def continue_reading(reader_id=None, now=None, path=None) -> list[dict]:
    """Books you're IN: a place past page 1, not finished, not hidden, touched in the last
    CONTINUE_DAYS. Page 1 is opening a book, not reading it — the web never saves it, but the
    apps did, and nine glances sat on Continue reading (the owner, 2026-10-18). Per reader."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=CONTINUE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at, p.page, p.updated_at, t.title AS tracked_title, s.title AS shelf_title FROM read_progress p JOIN books b ON b.id = p.book_id
            LEFT JOIN tracked_series t ON t.id = b.tracked_series_id LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
            WHERE p.reader_id = ? AND p.completed = 0 AND p.page > 1 AND p.dismissed = 0 AND p.updated_at >= ? AND b.missing_at IS NULL
            ORDER BY p.updated_at DESC LIMIT ?""", (reader_id, since, ROW_LIMIT)).fetchall()
        cards = [_card(conn, r, {"page": r["page"], "updated_at": r["updated_at"]}) for r in rows]
    # the list a book is being read from goes with it: it opens with the list around it
    from kometa import readlists
    ctx = readlists.context_for([c["book_id"] for c in cards], reader_id, path)
    for c in cards:
        if c["book_id"] in ctx:
            c["list_id"] = ctx[c["book_id"]]
    return cards


# Every column of books EXCEPT pages_json: the page lists (a few KB per opened
# book, 2.7 MB across a shelf) rode along on every On Deck query and nothing here
# reads them. Named columns halved _series_groups (16.7 ms -> 8.4 ms).
BOOK_COLS = "b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at"


def _series_groups(conn, reader_id):
    """Per series (tracked id or shelf id): books in issue order with progress."""
    rows = conn.execute("""
        SELECT b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at, p.completed, p.updated_at, p.page, t.title AS tracked_title, s.title AS shelf_title FROM books b
        LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
        LEFT JOIN tracked_series t ON t.id = b.tracked_series_id LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
        WHERE b.missing_at IS NULL
        ORDER BY b.number IS NULL, b.number, b.path""", (reader_id,)).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        key = ("t", r["tracked_series_id"]) if r["tracked_series_id"] else ("s", r["shelf_series_id"])
        if key[1] is None:
            continue
        groups.setdefault(key, []).append(r)
    return groups


NEAR_END_PAGES = 3          # this close to the end counts as through it for what's next (the owner, 2026-10-25)
NEAR_END_SHARE = 0.10       # ...or this share of the book, if that's more pages


def _through(b) -> bool:
    """Finished, or close enough to the end that the next one is up: the last 3 pages
    (or last 10%) are letters, ads and previews. The book itself stays on Continue."""
    if b["completed"] == 1:
        return True
    if not b["updated_at"] or not b["page_count"] or b["page"] is None:
        return False
    return b["page_count"] - b["page"] <= max(NEAR_END_PAGES, round(b["page_count"] * NEAR_END_SHARE))


def up_next(reader_id=None, path=None, groups=None) -> list[dict]:
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    out = []
    with db._connect(path) as conn:
        for key, books in (groups if groups is not None else _series_groups(conn, reader_id)).items():
            numbered = [b for b in books if b["number"] is not None]
            through = [b for b in numbered if _through(b)]
            if not through:
                continue
            last_done = max(b["number"] for b in through)
            last_read_at = max(b["updated_at"] for b in through)
            nxt = next((b for b in numbered if b["number"] > last_done and not b["completed"]), None)
            # finish one, the next one is up — whatever else in the series is open (a half-read
            # omnibus hid Superior Spider-Man #2 from BOTH rows, the owner, 2026-10-25). Only the
            # next issue itself, already started, skips Next: Continue (or its Not now) has it
            if nxt and not (nxt["updated_at"] and (nxt["page"] or 0) > 1):
                out.append(_card(conn, nxt, None, last_read_at=last_read_at,
                                 finished=sum(1 for b in numbered if b["completed"] == 1)))
    out.sort(key=lambda c: c["last_read_at"], reverse=True)
    return out[:ROW_LIMIT]


def coming_soon(reader_id=None, path=None, groups=None) -> list[dict]:
    """Series you're reading and caught up on (every numbered owned book
    finished) whose issue list has something you don't own yet."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    out = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db._connect(path) as conn:
        queue = {(q["tracked_series_id"], q["issue_number"]): q["state"] for q in conn.execute(
            "SELECT tracked_series_id, issue_number, state FROM download_queue WHERE state NOT IN ('done','failed','not_found','cancelled')")}
        for key, books in (groups if groups is not None else _series_groups(conn, reader_id)).items():
            if key[0] != "t":
                continue
            tid = key[1]
            numbered = [b for b in books if b["number"] is not None]
            # caught up = every numbered issue read through (a trade open alongside doesn't count)
            done = [b for b in numbered if _through(b)]
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
        if key[1] is None or key in seen:
            continue
        seen.add(key)
        # the card carries your place in it (a read one comes out dimmed and ticked), when the row has one
        prog = ({"page": r["page"], "completed": bool(r["completed"]), "updated_at": r["updated_at"]}
                if r["page"] is not None else None)
        out.append(_card(conn, r, prog, **{k: r[v] for k, v in field.items()}))
        if len(out) >= ROW_LIMIT:
            break
    return out


def recent_added(reader_id=None, path=None) -> list[dict]:
    """What landed on the shelf lately, by the FILE's own timestamp (the index
    date lies: the whole shelf was indexed this week). One card per series,
    its newest file — seven Deadly Duo issues in a night is one card. Read ones
    stay, wearing their progress: it's what's new, not what's left (the owner,
    2026-10-28 — finishing one made it vanish from under you)."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at, p.page, p.completed, p.updated_at FROM books b
            LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE b.missing_at IS NULL
            ORDER BY b.mtime DESC, b.number DESC LIMIT 400""", (reader_id,)).fetchall()
        return _one_per_series(conn, rows, mtime="mtime")


def recent_released(reader_id=None, path=None) -> list[dict]:
    """Owned issues by RELEASE date (the catalogue's store date), newest first,
    one card per series, read or not (see recent_added). What came out lately that you have."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at, i.store_date, p.page, p.completed, p.updated_at FROM issue_status i
            JOIN books b ON b.tracked_series_id = i.tracked_series_id AND b.number = i.number
            LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE i.owned = 1 AND i.store_date IS NOT NULL AND b.missing_at IS NULL
            ORDER BY i.store_date DESC, b.number DESC LIMIT 400""", (reader_id,)).fetchall()
        return _one_per_series(conn, rows, store_date="store_date")


# --- discovery rows ------------------------------------------------------------------
def _skey(series_id, shelf_id):
    return ("t", series_id) if series_id else ("s", shelf_id)


def list_rows(reader_id=None, now=None, path=None, seen: set | None = None) -> list[dict]:
    """'Next on {list}' for the lists you've been reading from lately: the entries
    from the first unfinished one on, in order. Owned entries are book cards;
    gaps are gap cards (the list page is where you deal with them)."""
    reader_id = reader_id or current_reader()
    from kometa.related import _resolved_lists
    from kometa.auth import OWNER
    from kometa import readlists
    path = path or DB_PATH
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=LIST_RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = seen if seen is not None else set()
    scored = []
    # the warm copy is every list resolved as the owner — his lists, his progress. Anyone
    # else: their own lists and the shared ones, resolved with THEIR progress (before
    # 2026-10-26 a reader's row followed the owner's reading)
    if reader_id == OWNER:
        lists = _resolved_lists(path)
    else:
        with db._connect(path) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM reading_lists WHERE owner = ? OR shared = 1", (reader_id,))]
        lists = []
        for lid in ids:
            try:
                lists.append(readlists.resolve(lid, path))
            except KeyError:
                pass
    for lst in lists:
        last = max((b["progress"]["updated_at"] for e in lst["entries"] for b in e["books"]
                    if b["progress"] and b["progress"].get("updated_at")), default=None)
        if last and last >= since:
            scored.append((last, lst))
    scored.sort(key=lambda x: x[0], reverse=True)
    rows, shown = [], set()
    with db._connect(path) as conn:
        for _, lst in scored:
            if len(rows) >= LIST_ROWS:
                break
            # where you ARE in the list: the book you touched last. Unfinished →
            # start on it; finished → start after it. (The first unfinished
            # entry is wrong for a 100-entry list you dipped into at #60.)
            flat = [(e, b) for e in lst["entries"] for b in (e["books"] or [None])]
            touched = [(i, b) for i, (e, b) in enumerate(flat)
                       if b and b["progress"] and b["progress"].get("updated_at")]
            i_last, b_last = max(touched, key=lambda t: t[1]["progress"]["updated_at"])
            start = i_last if not b_last["progress"]["completed"] else i_last + 1
            cards = []
            for e, b in flat[start:]:
                if b is None:
                    cards.append({"kind": "gap", "series": e["series"], "number": e["number"],
                                  "series_id": e.get("series_id"),   # a tracked run: the gap opens the issue's card
                                  "label": f"{e['series']} #{e['number']}" if e.get("number") else e["series"],
                                  "cover": e.get("cover"), "position": e["position"], "list_id": lst["id"]})
                elif not (b["progress"] and b["progress"]["completed"]) and b["id"] not in shown:
                    row = conn.execute("SELECT * FROM books WHERE id = ?", (b["id"],)).fetchone()
                    if row:
                        c = _card(conn, row, {"page": b["progress"]["page"]} if b["progress"] else None,
                                  kind="book", position=e["position"], list_id=lst["id"])
                        if len(e["books"]) > 1:
                            # a whole run on the list: every one of its issues said '7/10'
                            # (DC Absolute's Absolute Superman); the issue number says which
                            c["tag"] = c["label"]
                        c["label"] = f"{c['series']} {c['label']}"
                        cards.append(c)
                if len(cards) >= ROW_LIMIT:
                    break
            if len([c for c in cards if c["kind"] == "book"]) and len(cards) >= DISCOVERY_MIN:
                rows.append({"list_id": lst["id"], "name": lst["name"], "total": lst["total"], "cards": cards})
                shown.update(c["book_id"] for c in cards if c["kind"] == "book")
                seen.update(_skey(c.get("series_id"), c.get("shelf_id")) for c in cards if c["kind"] == "book")
    return rows


def rediscover(reader_id=None, now=None, path=None, seen: set | None = None, groups=None) -> list[dict]:
    """Series you read and then left: last touched 180+ days ago, unread books
    still here. The card is where you'd pick up — the stalled book, else the
    one after the last you finished. Most recently abandoned first."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    now = now or datetime.now(timezone.utc)
    before = (now - timedelta(days=REDISCOVER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = seen if seen is not None else set()
    out = []
    with db._connect(path) as conn:
        for key, books in (groups if groups is not None else _series_groups(conn, reader_id)).items():
            if key in seen:
                continue
            touched = [b for b in books if b["updated_at"]]
            if not touched:
                continue
            last = max(b["updated_at"] for b in touched)
            if last >= before:
                continue
            stalled = next((b for b in touched if b["completed"] == 0), None)
            pick = stalled
            if not pick:
                numbered = [b for b in books if b["number"] is not None]
                done = [b["number"] for b in numbered if b["completed"] == 1]
                after = max(done) if done else None
                pick = next((b for b in numbered if not b["completed"] and (after is None or b["number"] > after)), None)
            if pick:
                prog = {"page": pick["page"] or 0} if stalled else None
                out.append(_card(conn, pick, prog, last_read_at=last))
    out.sort(key=lambda c: c["last_read_at"], reverse=True)
    out = out[:ROW_LIMIT]
    if len(out) < DISCOVERY_MIN:
        return []
    seen.update(_skey(c["series_id"], c["shelf_id"]) for c in out)
    return out


def quick_reads(reader_id=None, path=None, seen: set | None = None) -> list[dict]:
    """Complete runs of ≤12 issues you've never opened: every catalogue issue
    owned, nothing still to come, no page of it read. Newest files first."""
    reader_id = reader_id or current_reader()
    path = path or DB_PATH
    seen = seen if seen is not None else set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    with db._connect(path) as conn:
        runs = conn.execute("""
            SELECT i.tracked_series_id AS tid, COUNT(*) AS n_issues, SUM(i.owned) AS n_owned,
                   MAX(COALESCE(i.store_date, '')) AS last_date
            FROM issue_status i JOIN tracked_series t ON t.id = i.tracked_series_id
            WHERE i.ignored = 0 AND COALESCE(t.kind, 'series') != 'arc'
            GROUP BY i.tracked_series_id
            HAVING n_issues BETWEEN 1 AND ? AND n_owned = n_issues""", (QUICK_MAX_ISSUES,)).fetchall()   # aliases NOT named like columns: SQLite's HAVING prefers the column
        runs = [r for r in runs if ("t", r["tid"]) not in seen and not (r["last_date"] and r["last_date"] > today)]
        # every candidate run's books in ONE query (was one per run: 523 a load)
        by_run: dict[int, list] = {}
        for i in range(0, len(runs), 500):
            chunk = [r["tid"] for r in runs[i:i + 500]]
            for b in conn.execute(f"""
                SELECT b.id, b.path, b.size, b.mtime, b.page_count, b.tracked_series_id, b.number, b.added_at, b.shelf_series_id, b.missing_at, p.updated_at FROM books b
                LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
                WHERE b.tracked_series_id IN ({",".join("?" * len(chunk))}) AND b.number IS NOT NULL AND b.missing_at IS NULL
                ORDER BY b.number, b.path""", (reader_id, *chunk)):
                by_run.setdefault(b["tracked_series_id"], []).append(b)
        for r in runs:
            books = by_run.get(r["tid"])
            if not books or any(b["updated_at"] for b in books):
                continue
            out.append(_card(conn, books[0], None, issues=r["n_issues"],
                             newest=max(b["mtime"] or 0 for b in books)))
    out.sort(key=lambda c: c["newest"], reverse=True)
    out = out[:ROW_LIMIT]
    if len(out) < DISCOVERY_MIN:
        return []
    seen.update(("t", c["series_id"]) for c in out)
    return out


@router.post("/api/books/{book_id}/dismiss")
def dismiss_book(book_id: int, undo: int = 0):
    """'Not now' on Continue reading. Place kept; cleared by the next read."""
    from fastapi import HTTPException
    if not db.set_dismissed(current_reader(), book_id, not undo, DB_PATH):
        raise HTTPException(404, "No progress for that book")
    return {"ok": True, "dismissed": not undo}


@router.get("/api/ondeck")
def on_deck():
    from kometa.marks import favourites
    from kometa.wantread import row as want_row
    # the whole shelf with progress, read once and shared (it was read three times)
    with db._connect(DB_PATH) as conn:
        groups = _series_groups(conn, current_reader())
    d = {"continue": continue_reading(), "next": up_next(groups=groups), "soon": coming_soon(groups=groups),
         "released": recent_released(), "added": recent_added(), "favourites": favourites(), "want": want_row()}
    # a series shows in one row only: the task rows claim theirs first
    seen = {_skey(c.get("series_id"), c.get("shelf_id")) for k in ("continue", "next", "soon", "released", "added")
            for c in d[k]}
    d["lists"] = list_rows(seen=seen)
    d["rediscover"] = rediscover(seen=seen, groups=groups)
    d["quick"] = quick_reads(seen=seen)
    return d
