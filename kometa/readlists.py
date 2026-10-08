"""Reading lists: an ORDER to read books in, across series.

v1 is CBL import (ComicRack's XML; the DieselTech community repo holds ~1,700
of them) resolved against YOUR shelf. A list is just ordered entries of
(series, number). Resolution happens on read, not at import — the shelf changes
under it (a Combine, a new download) and the list should simply be right the
next time you open it.

How an entry finds its books:
  - series title → a shelf series, by the punctuation-blind key (list says
    'Hellboy: Wake the Devil', folder says 'Hellboy - Wake the Devil').
  - a series that appears ONCE in the list is a collected edition standing for
    the whole run ('Hellboy: Seed of Destruction #1' in a TPB reading order is
    the trade; you hold #1–#4). It expands to every book of that run, in order.
  - a series that appears several times ('Sir Edward Grey, Witchfinder #2…#6')
    is being read volume by volume: each entry is the book with that number.
  - nothing on the shelf → 'not_on_shelf'. The entry stays in the order so the
    gap is visible; getting it is the acquisition side's job, later.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import kometa.db as db
from kometa.naming import norm_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
READER_ID = "me"


# --- storage ----------------------------------------------------------------------
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reading_lists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'cbl',
                source_ref  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reading_list_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id     INTEGER NOT NULL REFERENCES reading_lists(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL,
                series      TEXT NOT NULL,
                number      TEXT,
                volume      TEXT,
                year        TEXT,
                cv_series   TEXT,
                cv_issue    TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rli_list ON reading_list_items(list_id, position)")


class CblError(ValueError):
    pass


def parse_cbl(data: bytes) -> dict:
    """{name, items:[{series, number, volume, year, cv_series, cv_issue}]}.
    Tolerant of the two shapes in the wild (attributes on <Book>, or child
    elements); raises CblError on anything that isn't a reading list."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CblError(f"Not XML: {e}")
    if root.tag != "ReadingList":
        raise CblError("Not a ComicRack reading list (no <ReadingList> root)")
    name = (root.findtext("Name") or "").strip() or "Reading list"
    items = []
    for b in root.iter("Book"):
        g = lambda k: (b.get(k) or b.findtext(k) or "").strip()
        series = g("Series")
        if not series:
            continue
        dbn = b.find("Database")
        items.append({"series": series, "number": g("Number") or None, "volume": g("Volume") or None,
                      "year": g("Year") or None,
                      "cv_series": (dbn.get("Series") if dbn is not None else None) or None,
                      "cv_issue": (dbn.get("Issue") if dbn is not None else None) or None})
    if not items:
        raise CblError("The list has no books in it")
    return {"name": name, "items": items}


def import_cbl(data: bytes, source_ref: str | None = None, path=None) -> int:
    path = path or DB_PATH
    ensure_tables(path)
    parsed = parse_cbl(data)
    with db._connect(path) as conn:
        # same name again = re-import: replace, keep the id so links survive
        row = conn.execute("SELECT id FROM reading_lists WHERE name = ?", (parsed["name"],)).fetchone()
        if row:
            lid = row[0]
            conn.execute("DELETE FROM reading_list_items WHERE list_id = ?", (lid,))
            conn.execute("UPDATE reading_lists SET source_ref = ?, created_at = datetime('now') WHERE id = ?", (source_ref, lid))
        else:
            lid = conn.execute("INSERT INTO reading_lists (name, source, source_ref) VALUES (?, 'cbl', ?)",
                               (parsed["name"], source_ref)).lastrowid
        conn.executemany(
            "INSERT INTO reading_list_items (list_id, position, series, number, volume, year, cv_series, cv_issue) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(lid, i + 1, it["series"], it["number"], it["volume"], it["year"], it["cv_series"], it["cv_issue"])
             for i, it in enumerate(parsed["items"])])
    logger.info(f"Reading list imported: {parsed['name']!r}, {len(parsed['items'])} entries")
    return lid


def get_lists(path=None) -> list[dict]:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM reading_list_items i WHERE i.list_id = l.id) AS entries "
            "FROM reading_lists l ORDER BY name")]


def get_items(list_id: int, path=None) -> list[dict]:
    with db._connect(path or DB_PATH) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reading_list_items WHERE list_id = ? ORDER BY position", (list_id,))]


def delete_list(list_id: int, path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("DELETE FROM reading_list_items WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM reading_lists WHERE id = ?", (list_id,))


# --- resolution -------------------------------------------------------------------
_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
_AND_OTHERS = re.compile(r"\s+and\s+others?$", re.I)


def _keys(title: str) -> set[str]:
    """Every spelling a shelf folder might use for this title. 'B.P.R.D.' is
    'b p r d' under norm_key and 'bprd' under the tighter key — both count."""
    t = _YEAR.sub("", title or "").strip()
    out = set()
    for v in (t, _AND_OTHERS.sub("", t)):
        k = norm_key(v)
        if k:
            out.add(k)
            out.add(re.sub(r"\s+", "", k))
    return out


def _num(n) -> float | None:
    try:
        return float(str(n).strip()) if n not in (None, "") else None
    except ValueError:
        return None


def _shelf_index(path) -> dict[str, dict]:
    """key → shelf series (first wins on a collision, alphabetical)."""
    idx: dict[str, dict] = {}
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, tracked_series_id, folder_path, book_count FROM shelf_series ORDER BY title")]
        tracked = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tracked_series")}
    for s in rows:
        names = {s["title"]}
        if s.get("tracked_series_id") in tracked:
            names.add(tracked[s["tracked_series_id"]])
        for n in names:
            for k in _keys(n):
                idx.setdefault(k, s)
    return idx


def _books(shelf_id: int, path) -> list[dict]:
    rows = db.shelf_books(shelf_id, READER_ID, path)
    # one book per number: the CBZ beats the CBR, else the newest file. The
    # Midnight Circus folder holds '#001 (2013).cbz' AND '#001.cbz'.
    rows.sort(key=lambda b: (b["number"] if b["number"] is not None else 1e9,
                             0 if b["path"].lower().endswith(".cbz") else 1, -(b.get("mtime") or 0)))
    seen, out = set(), []
    for b in rows:
        key = b["number"]
        if key is not None and key in seen:
            continue
        seen.add(key)
        out.append(_book_view(b))
    return out


def _book_view(b: dict) -> dict:
    import os
    n = b.get("number")
    label = f"#{int(n) if n == int(n) else n}" if n is not None else os.path.splitext(os.path.basename(b["path"]))[0]
    prog = None
    if b.get("progress_page") is not None:
        prog = {"page": b["progress_page"], "completed": bool(b.get("completed")), "updated_at": b.get("updated_at")}
    return {"id": b["id"], "number": n, "label": label, "page_count": b.get("page_count"), "progress": prog}


def resolve(list_id: int, path=None) -> dict:
    """The list with every entry resolved to books on the shelf (or not)."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        row = conn.execute("SELECT * FROM reading_lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        raise KeyError(list_id)
    items = get_items(list_id, path)
    idx = _shelf_index(path)
    per_series: dict[str, int] = {}
    for it in items:
        per_series[norm_key(it["series"])] = per_series.get(norm_key(it["series"]), 0) + 1
    out, books_cache = [], {}
    owned = read = 0
    for it in items:
        hit = next((idx[k] for k in _keys(it["series"]) if k in idx), None)
        entry = {"position": it["position"], "series": it["series"], "number": it["number"],
                 "year": it["year"], "status": "not_on_shelf", "shelf_id": None, "series_id": None, "books": []}
        if hit:
            entry["shelf_id"], entry["series_id"] = hit["id"], hit.get("tracked_series_id")
            if hit["id"] not in books_cache:
                books_cache[hit["id"]] = _books(hit["id"], path)
            books = books_cache[hit["id"]]
            n = _num(it["number"])
            if per_series[norm_key(it["series"])] > 1 and n is not None:
                picked = [b for b in books if b["number"] == n]
                entry["status"] = "owned" if picked else "missing"
            else:
                # the whole run — and when the folder holds singles AND the trade
                # of the same story ('#1…#4' plus 'TPB (2022)'), the singles are
                # the run; the trade would be the same pages again
                numbered = [b for b in books if b["number"] is not None]
                picked = numbered or books
                entry["status"] = "owned" if picked else "missing"
                entry["expanded"] = len(picked) > 1
            entry["books"] = picked
        if entry["status"] == "owned":
            owned += 1
            if entry["books"] and all(b["progress"] and b["progress"]["completed"] for b in entry["books"]):
                read += 1
        out.append(entry)
    return {"id": row["id"], "name": row["name"], "source": row["source"], "source_ref": row["source_ref"],
            "entries": out, "total": len(out), "owned": owned, "read": read,
            "continue": _continue_point(out)}


def order(list_id: int, path=None) -> list[int]:
    """Every book of the list in reading order — the reader's 'next' walks this."""
    return [b["id"] for e in resolve(list_id, path)["entries"] for b in e["books"]]


def _continue_point(entries: list[dict]) -> int | None:
    """The first book not yet finished, in order — what 'Continue' opens."""
    for e in entries:
        for b in e["books"]:
            if not (b["progress"] and b["progress"]["completed"]):
                return b["id"]
    return None


def next_book(list_id: int, after: int, path=None) -> int | None:
    seq = order(list_id, path)
    try:
        i = seq.index(after)
    except ValueError:
        return None
    return seq[i + 1] if i + 1 < len(seq) else None


# --- API --------------------------------------------------------------------------
class ImportUrlRequest(BaseModel):
    url: str


@router.get("/api/readlists")
def api_lists():
    return get_lists()


@router.get("/api/readlists/{list_id}")
def api_list(list_id: int):
    try:
        return resolve(list_id)
    except KeyError:
        raise HTTPException(404, "No such reading list")


@router.get("/api/readlists/{list_id}/next")
def api_next(list_id: int, after: int):
    nb = next_book(list_id, after)
    return {"book_id": nb}


@router.delete("/api/readlists/{list_id}", status_code=204)
def api_delete(list_id: int):
    delete_list(list_id)


@router.post("/api/readlists/import")
async def api_import_body(request: Request, name: str | None = None):
    """The .cbl's XML as the request body (the page reads the file and posts
    its text — no multipart, which the image doesn't ship)."""
    data = await request.body()
    try:
        lid = import_cbl(data, source_ref=name)
    except CblError as e:
        raise HTTPException(400, str(e))
    return {"id": lid}


@router.post("/api/readlists/import-url")
def api_import_url(req: ImportUrlRequest):
    """A raw .cbl link — GitHub 'blob' links are rewritten to raw."""
    import requests
    url = re.sub(r"^https://github\.com/([^/]+/[^/]+)/blob/", r"https://raw.githubusercontent.com/\1/", req.url.strip())
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(400, f"Couldn't fetch that: {e}")
    try:
        lid = import_cbl(r.content, source_ref=req.url.strip())
    except CblError as e:
        raise HTTPException(400, str(e))
    return {"id": lid}
