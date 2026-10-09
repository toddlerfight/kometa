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
                cv_issue    TEXT,
                file        TEXT
            )""")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reading_list_items)")]
        if "file" not in cols:
            conn.execute("ALTER TABLE reading_list_items ADD COLUMN file TEXT")
        if "cover_url" not in cols:
            # a catalogue cover for an entry that isn't on the shelf: NULL = not
            # looked yet, '' = looked and nothing, else the image URL (cached)
            conn.execute("ALTER TABLE reading_list_items ADD COLUMN cover_url TEXT")
        if "cover_checked_at" not in cols:
            # when the catalogue was last asked: a miss is retried after COVER_RETRY_DAYS,
            # a rotten URL is cleared by the proxy so the next open asks again
            conn.execute("ALTER TABLE reading_list_items ADD COLUMN cover_checked_at TEXT")
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


def _forget_resolved():
    """Related keeps resolved lists warm for a couple of minutes; a list changing
    shouldn't wait that long."""
    try:
        from kometa import related
        related._lists_cache["value"] = None
    except Exception:
        pass


def save_list(name: str, items: list[dict], source: str, source_ref: str | None, path=None) -> int:
    _forget_resolved()
    """Write a list. The same name again is a re-import: replaced in place, id
    kept so links to it survive."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        row = conn.execute("SELECT id FROM reading_lists WHERE name = ?", (name,)).fetchone()
        if row:
            lid = row[0]
            conn.execute("DELETE FROM reading_list_items WHERE list_id = ?", (lid,))
            conn.execute("UPDATE reading_lists SET source = ?, source_ref = ?, created_at = datetime('now') WHERE id = ?",
                         (source, source_ref, lid))
        else:
            lid = conn.execute("INSERT INTO reading_lists (name, source, source_ref) VALUES (?, ?, ?)",
                               (name, source, source_ref)).lastrowid
        conn.executemany(
            "INSERT INTO reading_list_items (list_id, position, series, number, volume, year, cv_series, cv_issue, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(lid, i + 1, it["series"], it.get("number"), it.get("volume"), it.get("year"),
              it.get("cv_series"), it.get("cv_issue"), it.get("file")) for i, it in enumerate(items)])
    logger.info(f"Reading list saved: {name!r} ({source}), {len(items)} entries")
    return lid


def import_cbl(data: bytes, source_ref: str | None = None, path=None) -> int:
    parsed = parse_cbl(data)
    return save_list(parsed["name"], parsed["items"], "cbl", source_ref, path)


def import_komga(komga=None, path=None) -> list[dict]:
    """Every Komga read list → a list here, one entry per book, the FILE NAME
    as the hint — Komga's metadata.number is a counter, not the issue (its
    'Batman: Knightfall #1' is a nine-volume trade). The title+number are kept
    beside it for the day the file is renamed."""
    import os
    from kometa import sources
    from kometa.naming import parse_issue_number
    komga = komga or sources.komga()
    if not komga:
        raise RuntimeError("Komga isn't configured")
    out = []
    for rl in _pages(komga, "/api/v1/readlists"):
        books = _pages(komga, f"/api/v1/readlists/{rl['id']}/books")
        items = []
        for b in books:
            fn = os.path.basename(b.get("url") or "")
            series = b.get("seriesTitle") or ""
            n = parse_issue_number(fn, series) if fn else None
            if n is None:
                n = (b.get("metadata") or {}).get("number")
            items.append({"series": series, "number": str(n) if n is not None else None, "file": fn or None})
        if not items:
            continue
        lid = save_list(rl["name"], items, "komga", rl["id"], path)
        out.append({"id": lid, "name": rl["name"], "entries": len(items)})
    return out


def import_komga_collections(komga=None, min_series: int = 2, path=None) -> list[dict]:
    """Komga's collections are reading lists at series grain — 'Murphyverse' is
    ten runs in an order. One entry per series, no number, so each resolves to
    its whole run in order (the rule the TPB reading orders already use).
    A one-series collection isn't an order, so it's skipped."""
    from kometa import sources
    komga = komga or sources.komga()
    if not komga:
        raise RuntimeError("Komga isn't configured")
    out = []
    for coll in _pages(komga, "/api/v1/collections"):
        series = _pages(komga, f"/api/v1/collections/{coll['id']}/series")
        items = [{"series": s.get("metadata", {}).get("title") or s.get("name") or "", "number": None}
                 for s in series]
        items = [it for it in items if it["series"]]
        if len(items) < min_series:
            continue
        lid = save_list(coll["name"], items, "komga", coll["id"], path)
        out.append({"id": lid, "name": coll["name"], "entries": len(items)})
    return out


def _pages(komga, url: str, size: int = 200) -> list[dict]:
    rows, page = [], 0
    while True:
        r = komga._get(url, params={"size": size, "page": page})
        rows += r.get("content", [])
        if r.get("last", True) or not r.get("content"):
            return rows
        page += 1


def get_lists(path=None) -> list[dict]:
    """Every list with the card facts: owned/total/read and a cover (the first
    book on the shelf, in order). Resolved live, same as the list page."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM reading_list_items i WHERE i.list_id = l.id) AS entries "
            "FROM reading_lists l ORDER BY name")]
    for r in rows:
        try:
            res = resolve(r["id"], path)
        except KeyError:
            continue
        first = next((b for e in res["entries"] for b in e["books"]), None)
        r.update(total=res["total"], owned=res["owned"], read=res["read"], books=res["books"],
                 books_read=res["books_read"], books_reading=res["books_reading"], cover_book_id=first["id"] if first else None)
    return rows


def get_items(list_id: int, path=None) -> list[dict]:
    with db._connect(path or DB_PATH) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reading_list_items WHERE list_id = ? ORDER BY position", (list_id,))]


def delete_list(list_id: int, path=None):
    _forget_resolved()
    with db._connect(path or DB_PATH) as conn:
        conn.execute("DELETE FROM reading_list_items WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM reading_lists WHERE id = ?", (list_id,))


# --- resolution -------------------------------------------------------------------
_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\s*-?\s*(?:(?:19|20)\d{2})?\)\s*$")   # '(2020)', '(2020-)', '(2020-2021)'
_TRADE_TAIL = re.compile(r"\s*(?:\((?:19|20)\d{2}\)|tpb|hc|\d{2,3})\s*$", re.I)
_AND_OTHERS = re.compile(r"\s+and\s+others?$", re.I)


def _keys(title: str) -> set[str]:
    """Every spelling a shelf folder might use for this title. 'B.P.R.D.' is
    'b p r d' under norm_key and 'bprd' under the tighter key — both count."""
    t = _YEAR.sub("", title or "").strip()
    out = set()
    # 'Tank Girl: Vol. 2' is the shelf's 'Tank Girl 2'; '…: TPB' is the file without it
    variants = {t, _AND_OTHERS.sub("", t), re.sub(r"\bvol(?:ume)?\.?\s*", "", t, flags=re.I),
                re.sub(r"[\s:,-]*\b(tpb|hc|gn)\b\s*$", "", t, flags=re.I)}
    for v in variants:
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


def _by_file(path, names: list[str]) -> dict[str, dict]:
    """basename → book row (with progress), for the names that are on the shelf.
    One pass over the books table matched in Python: SQLite can't index a
    basename, and 7k rows is nothing."""
    import os
    if not names:
        return {}
    want, out = set(names), {}
    with db._connect(path) as conn:
        for r in conn.execute("""
            SELECT b.id, b.path, b.number, b.page_count, b.size, b.mtime, b.shelf_series_id, b.tracked_series_id,
                   p.page AS progress_page, p.completed, p.updated_at
            FROM books b LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
        """, (READER_ID,)):
            bn = os.path.basename(r["path"])
            if bn in want and bn not in out:
                out[bn] = dict(r)
    return out


def _subtitle_book(name: str, idx: dict, path, books_cache: dict):
    """('Series - Subtitle' entry) → (shelf series, the book whose file name
    carries the subtitle), or None. Splits at the LAST separator so a run that
    itself has a dash ('Batman - One Bad Day') still finds its prefix."""
    import os
    parts = re.split(r"\s+-\s+|:\s+", _YEAR.sub("", name).strip())
    if len(parts) < 2:
        return None
    for cut in range(len(parts) - 1, 0, -1):
        prefix, sub = " - ".join(parts[:cut]), " ".join(parts[cut:])
        sh = next((idx[k] for k in _keys(prefix) if k in idx), None)
        if not sh:
            continue
        if sh["id"] not in books_cache:
            books_cache[sh["id"]] = _books(sh["id"], path)
        want = norm_key(sub)
        with db._connect(path) as conn:
            names = {r["id"]: os.path.basename(r["path"]) for r in conn.execute(
                "SELECT id, path FROM books WHERE shelf_series_id = ?", (sh["id"],))}
        for b in books_cache[sh["id"]]:
            if want and want in norm_key(os.path.splitext(names.get(b["id"], ""))[0]):
                return sh, b
    return None


def _complete_run(name: str, idx: dict, path, books_cache: dict):
    """('Hellboy in Hell: The Descent' entry) → (shelf series, its books) when the
    base run before the subtitle is on the shelf COMPLETE: every catalogue issue
    owned, or an untracked folder with files. A trade of a run you hold whole is
    pages you already have — the resolver says owned, through the run, rather
    than send Get off to buy them again. Only the base is matched; a partial
    run stays missing, since the trade's own issues could be the gap."""
    parts = re.split(r":\s+|\s+-\s+", _YEAR.sub("", name).strip())
    if len(parts) < 2:
        return None
    sh = next((idx[k] for k in _keys(parts[0]) if k in idx), None)
    if not sh:
        return None
    if sh["id"] not in books_cache:
        books_cache[sh["id"]] = _books(sh["id"], path)
    books = books_cache[sh["id"]]
    if not books:
        return None
    if sh.get("tracked_series_id"):
        issues = db.get_issues_for_series(sh["tracked_series_id"], path)
        if not issues or not all(i.get("owned") for i in issues):
            return None
    numbered = [b for b in books if b["number"] is not None]
    return sh, (numbered or books)


COVER_RETRY_DAYS = 7


def _cover_pending(it: dict) -> bool:
    """NULL = never asked; '' = a miss, asked again once it's a week old."""
    if it.get("cover_url") is None:
        return True
    if it.get("cover_url") == "":
        from datetime import datetime, timedelta
        chk = it.get("cover_checked_at")
        try:
            return not chk or datetime.strptime(chk[:19], "%Y-%m-%dT%H:%M:%S") < datetime.utcnow() - timedelta(days=COVER_RETRY_DAYS)
        except ValueError:
            return True
    return False


def _book_view(b: dict) -> dict:
    import os
    n = b.get("number")
    label = f"#{int(n) if n == int(n) else n}" if n is not None else os.path.splitext(os.path.basename(b["path"]))[0]
    prog = None
    if b.get("progress_page") is not None:
        prog = {"page": b["progress_page"], "completed": bool(b.get("completed")), "updated_at": b.get("updated_at")}
    return {"id": b["id"], "number": n, "label": label, "page_count": b.get("page_count"), "progress": prog}


def _trade_index(path) -> dict[str, list[dict]]:
    """key → books whose FILE NAME is the title: a trade filed under its run keeps
    its name ('Dark Nights - Metal - The Deluxe Edition (2018).cbz' in the Metal
    folder), so a list entry naming the deluxe edition finds the file, not the
    series. Built once per resolve."""
    import os
    idx: dict[str, list[dict]] = {}
    with db._connect(path) as conn:
        for r in conn.execute("""
            SELECT b.id, b.path, b.number, b.page_count, b.size, b.mtime, b.shelf_series_id, b.tracked_series_id,
                   p.page AS progress_page, p.completed, p.updated_at
            FROM books b LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE b.number IS NULL""", (READER_ID,)):
            stem = os.path.splitext(os.path.basename(r["path"]))[0]
            # both the name as filed and the name with its tail trimmed:
            # 'Fables - The Deluxe Edition - Book 09' answers to either
            for st in {stem, _TRADE_TAIL.sub("", stem), _TRADE_TAIL.sub("", _TRADE_TAIL.sub("", stem))}:
                for k in _keys(st):
                    idx.setdefault(k, []).append(dict(r))
    return idx


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
    by_file = _by_file(path, [it["file"] for it in items if it.get("file")])
    trades = None
    from kometa.combine import _numbered
    for it in items:
        series_name, want_n = it["series"], _num(it["number"])
        # 'Tank Girl - Skidmarks 1 of 4 - …' names one issue of a run that has since
        # been combined: the run is the base, the number is in the name
        nb = _numbered(series_name)
        if nb and not any(k in idx for k in _keys(series_name)):
            series_name, want_n = nb[0], float(nb[1])
        hit = next((idx[k] for k in _keys(series_name) if k in idx), None)
        entry = {"position": it["position"], "series": it["series"], "number": it["number"],
                 "year": it["year"], "status": "not_on_shelf", "shelf_id": None, "series_id": None, "books": [],
                 "item_id": it["id"], "cover": f"/api/readlists/{list_id}/items/{it['id']}/cover" if it.get("cover_url") else None,
                 "cover_pending": _cover_pending(it)}
        bf = by_file.get(it.get("file") or "")
        if bf:
            # the file itself is on the shelf — no guessing from the title
            entry.update(status="owned", shelf_id=bf["shelf_series_id"], series_id=bf.get("tracked_series_id"),
                         books=[_book_view(bf)])
        elif not hit and (trades if trades is not None else (trades := _trade_index(path))) and \
                any(k in trades for k in _keys(series_name)):
            # a collected edition filed under its run: the file carries the name
            rows = next(trades[k] for k in _keys(series_name) if k in trades)
            rows.sort(key=lambda b: b["path"])
            entry.update(status="owned", shelf_id=rows[0]["shelf_series_id"], series_id=rows[0].get("tracked_series_id"),
                         books=[_book_view(b) for b in rows], expanded=len(rows) > 1)
        elif not hit and _subtitle_book(series_name, idx, path, books_cache) is not None:
            # 'Batman - One Bad Day - The Riddler': a one-shot Combine folded into
            # a run as '#001 - The Riddler'. The prefix is the run, the tail is in
            # the file name.
            sh, book = _subtitle_book(series_name, idx, path, books_cache)
            entry.update(status="owned", shelf_id=sh["id"], series_id=sh.get("tracked_series_id"), books=[book])
        elif not hit and (cr := _complete_run(series_name, idx, path, books_cache)) is not None:
            # a trade of a run held whole: the pages are on the shelf, in the run
            sh, books = cr
            entry.update(status="owned", shelf_id=sh["id"], series_id=sh.get("tracked_series_id"),
                         books=[_book_view(b) for b in books], expanded=len(books) > 1, via_run=sh["title"])
        elif hit and nb and want_n is not None and series_name != it["series"]:
            # the numbered-folder shape: that one issue of the combined run
            if hit["id"] not in books_cache:
                books_cache[hit["id"]] = _books(hit["id"], path)
            picked = [b for b in books_cache[hit["id"]] if b["number"] == want_n]
            entry.update(shelf_id=hit["id"], series_id=hit.get("tracked_series_id"),
                         status="owned" if picked else "missing", books=picked)
        elif hit:
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
    books = [b for e in out for b in e["books"]]
    return {"id": row["id"], "name": row["name"], "source": row["source"], "source_ref": row["source_ref"],
            "entries": out, "total": len(out), "owned": owned, "read": read,
            # the chips: HAVE/MISSING count entries, READ/READING count books you have
            "books": len(books),
            "books_read": sum(1 for b in books if b["progress"] and b["progress"]["completed"]),
            "books_reading": sum(1 for b in books if b["progress"] and not b["progress"]["completed"]),
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


# --- covers for the gaps -------------------------------------------------------------
def fill_covers(list_id: int, path=None, cv=None, metron_search=None, metron_issues=None, limit_metron: int = 40) -> dict:
    """Give every not-on-shelf entry a catalogue cover, once. ComicVine first —
    a CBL names the issue id, so it's one batched lookup for the whole list —
    then Metron by title for the rest (slow, throttled, capped per call). A
    miss is remembered as '' so a list isn't re-asked every open."""
    path = path or DB_PATH
    ensure_tables(path)
    res = resolve(list_id, path)
    todo = [e for e in res["entries"] if e["status"] != "owned" and e["cover_pending"]]
    if not todo:
        return {"filled": 0, "missed": 0, "left": 0}
    items = {it["id"]: it for it in get_items(list_id, path)}
    found: dict[int, str] = {}
    # 1. ComicVine by issue id, in one batch
    if cv is None:
        from kometa import sources
        try:
            cv = sources.comicvine()
        except Exception:
            cv = None
    ids = {e["item_id"]: items[e["item_id"]].get("cv_issue") for e in todo}
    if cv and any(ids.values()):
        meta = cv.get_issues_meta([v for v in ids.values() if v])
        for iid, cvi in ids.items():
            url = cvi and (meta.get(str(cvi)) or {}).get("image_url")
            if url:
                found[iid] = url
    # 2. Metron by title for what's left, a few per call
    rest = [e for e in todo if e["item_id"] not in found][:limit_metron]
    if rest:
        from kometa import metron_client
        search = metron_search or (metron_client.search_series if metron_client.configured() else None)
        issues = metron_issues or metron_client.series_issues
        for e in rest:
            if not search:
                break
            try:
                rows = search(_YEAR.sub("", e["series"]))
                hit = next((r for r in rows if norm_key(r["title"]) == norm_key(_YEAR.sub("", e["series"]))), None) or (rows[0] if rows else None)
                img = None
                if hit:
                    n = _num(e["number"])
                    for i in issues(hit["id"]):
                        if i.get("image") and (n is None or i["number"] == n or img is None):
                            img = i["image"]
                            if n is None or i["number"] == n:
                                break
                found[e["item_id"]] = img or ""
            except Exception as ex:
                logger.info(f"Reading list cover: Metron skipped {e['series']!r}: {ex}")
                break
    with db._connect(path) as conn:
        for iid, url in found.items():
            conn.execute("UPDATE reading_list_items SET cover_url = ?, cover_checked_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (url, iid))
    filled = sum(1 for u in found.values() if u)
    return {"filled": filled, "missed": len(found) - filled, "left": len(todo) - len(found)}


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


@router.post("/api/readlists/{list_id}/covers")
def api_fill_covers(list_id: int):
    """Covers for the gaps: ComicVine in one batch now, a slice of Metron now,
    the rest on the next call. The page calls this until 'left' is 0."""
    try:
        return fill_covers(list_id)
    except KeyError:
        raise HTTPException(404, "No such reading list")


@router.get("/api/readlists/{list_id}/items/{item_id}/cover")
def api_item_cover(list_id: int, item_id: int):
    from kometa.thumbnails import _cached_image_response
    with db._connect(DB_PATH) as conn:
        r = conn.execute("SELECT cover_url FROM reading_list_items WHERE id = ? AND list_id = ?", (item_id, list_id)).fetchone()
    if not r or not r[0]:
        raise HTTPException(404)
    try:
        return _cached_image_response(r[0])
    except HTTPException:
        # the URL rotted: forget it, so the next open asks the catalogue again
        with db._connect(DB_PATH) as conn:
            conn.execute("UPDATE reading_list_items SET cover_url = NULL WHERE id = ?", (item_id,))
        raise


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


@router.post("/api/readlists/import-komga")
def api_import_komga():
    """Komga's read lists AND its collections — both are orders to read in."""
    try:
        return import_komga() + import_komga_collections()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Komga didn't answer: {e}")


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
