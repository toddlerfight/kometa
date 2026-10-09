"""OPDS 1.2 + Page Streaming Extension: the shelf for readers that aren't ours.

The cheap test from the spec (docs/reader-spec.md, point 7): before building a
native reader, let Panels and friends browse, stream and download from Kometa
over the tailnet. Atom feeds, nothing clever — a navigation root, every series,
each series' books as an acquisition feed, keep-reading and recently-added
shortcuts, and search. Every book carries a CBZ/CBR download link and a PSE
stream link (0-based pages, optional maxWidth) onto the reader's own page
pipeline. pse:lastRead reports what the web reader knows; PSE page fetches
do NOT write progress yet — that's the next, separate decision.
"""
import logging
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
READER_ID = "me"
PAGE = 100
NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MEDIA = {".cbz": "application/vnd.comicbook+zip", ".cbr": "application/vnd.comicbook-rar",
         ".cb7": "application/x-7z-compressed", ".pdf": "application/pdf"}
_YEAR = re.compile(r"\s*\((\d{4})\)\s*$")


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(s: str | None) -> str:
    if not s:
        return _now()
    s = s.replace(" ", "T")
    return s if s.endswith("Z") else s + "Z"


def _feed(kind: str, fid: str, title: str, links: list[str], entries: list[str], self_href: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" '
            f'xmlns:pse="http://vaemendis.net/opds-pse/ns" xmlns:dc="http://purl.org/dc/terms/">\n'
            f'<id>{escape(fid)}</id><title>{escape(title)}</title><updated>{_now()}</updated>'
            f'<author><name>Kometa</name></author>\n'
            f'<link rel="self" href={quoteattr(self_href)} type={quoteattr(kind)}/>\n'
            f'<link rel="start" href="/opds" type={quoteattr(NAV)}/>\n'
            f'<link rel="search" href="/opds/opensearch.xml" type="application/opensearchdescription+xml"/>\n'
            + "\n".join(links) + "\n" + "\n".join(entries) + "\n</feed>")


def _nav_entry(eid: str, title: str, href: str, summary: str = "", kind: str = ACQ) -> str:
    return (f'<entry><id>{escape(eid)}</id><title>{escape(title)}</title><updated>{_now()}</updated>'
            f'<link rel="subsection" href={quoteattr(href)} type={quoteattr(kind)}/>'
            + (f'<content type="text">{escape(summary)}</content>' if summary else '') + '</entry>')


def _page_count(b: dict, path) -> int:
    """The reader fills page_count the first time a book is opened; almost every
    book on a fresh shelf hasn't been. PSE needs a count up front, so an
    unopened book gets a cheap one from the archive's entry list (central
    directory only — no page is read) and remembers it."""
    if b.get("page_count"):
        return b["page_count"]
    from kometa.naming import _archive_entry_names, _IMAGE_EXTS
    names = _archive_entry_names(b["path"]) or []
    n = sum(1 for x in names if x.lower().endswith(_IMAGE_EXTS)
            and not x.startswith("__MACOSX") and not os.path.basename(x).startswith("."))
    if n:
        with db._connect(path) as conn:
            conn.execute("UPDATE books SET page_count = ? WHERE id = ? AND (page_count IS NULL OR page_count = 0)", (n, b["id"]))
    return n


def _label(b: dict) -> str:
    n = b.get("number")
    if n is not None:
        return f"#{int(n) if n == int(n) else n}"
    return os.path.splitext(os.path.basename(b["path"]))[0]


def book_entry(b: dict, series_title: str, progress: dict | None = None, path=None) -> str:
    """One Atom entry: cover, thumbnail, download, and the PSE stream link.
    PSE page numbers are 0-based (the template's {pageNumber}); the reader's
    own route is 1-based, so /opds/books/{id}/pages/{n} bridges the two."""
    bid = b["id"]
    media = MEDIA.get(os.path.splitext(b["path"])[1].lower(), "application/octet-stream")
    title = f"{series_title} {_label(b)}" if b.get("number") is not None else _label(b)
    count = _page_count(b, path or DB_PATH)
    pse = f'<link rel="http://vaemendis.net/opds-pse/stream" type="image/jpeg" ' \
          f'href="/opds/books/{bid}/pages/{{pageNumber}}?maxWidth={{maxWidth}}" pse:count="{count}"'
    if progress and progress.get("page"):
        pse += f' pse:lastRead="{int(progress["page"])}" pse:lastReadDate="{escape(_iso(progress.get("updated_at")))}"'
    pse += "/>"
    return (f'<entry><id>urn:kometa:book:{bid}</id><title>{escape(title)}</title>'
            f'<updated>{escape(_iso(b.get("added_at")))}</updated>'
            f'<link rel="http://opds-spec.org/image" href="/api/books/{bid}/cover" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/image/thumbnail" href="/api/books/{bid}/cover" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/acquisition" href="/opds/books/{bid}/file" type={quoteattr(media)}/>'
            f'{pse}'
            f'<content type="text">{escape(series_title)} · {count or "?"} pages</content></entry>')


def _progress_for(book_ids: list[int], path) -> dict[int, dict]:
    if not book_ids:
        return {}
    with db._connect(path) as conn:
        q = ",".join("?" * len(book_ids))
        return {r["book_id"]: dict(r) for r in conn.execute(
            f"SELECT book_id, page, completed, updated_at FROM read_progress WHERE reader_id = ? AND book_id IN ({q})",
            (READER_ID, *book_ids))}


def _books_sql(path, where: str, params: tuple, order: str, limit: int | None = None) -> list[dict]:
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT b.id, b.path, b.number, b.page_count, b.added_at, b.shelf_series_id,
                       s.title AS series_title FROM books b
                LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
                WHERE {where} ORDER BY {order}""" + (f" LIMIT {int(limit)}" if limit else ""), params)]


def _entries(books: list[dict], path) -> list[str]:
    prog = _progress_for([b["id"] for b in books], path)
    return [book_entry(b, b.get("series_title") or _YEAR.sub("", os.path.basename(os.path.dirname(b["path"]))),
                       prog.get(b["id"]), path) for b in books if os.path.exists(b["path"])]


# --- feeds ------------------------------------------------------------------------
def root_feed() -> str:
    return _feed(NAV, "urn:kometa:opds:root", "Kometa", [], [
        _nav_entry("urn:kometa:opds:keep-reading", "Keep reading", "/opds/keep-reading", "Where you left off"),
        _nav_entry("urn:kometa:opds:next", "Next", "/opds/ondeck/next", "The next unread issue in series you're reading"),
        _nav_entry("urn:kometa:opds:favourites", "Favourites", "/opds/ondeck/favourites", "What you starred"),
        _nav_entry("urn:kometa:opds:lists", "Reading lists", "/opds/lists", "Each list, in order, what's on the shelf", NAV),
        _nav_entry("urn:kometa:opds:recent", "Recently added", "/opds/recent", "Newest files on the shelf"),
        _nav_entry("urn:kometa:opds:az", "Series by letter", "/opds/az", "A to Z, one letter at a time", NAV),
        _nav_entry("urn:kometa:opds:series", "All series", "/opds/series", "Every series on the shelf", NAV),
    ], "/opds")


def _letter(title: str) -> str:
    t = re.sub(r"^(the|a|an)\s+", "", title.strip().lstrip("-–— ").strip(), flags=re.I)
    c = t[:1].upper()
    return c if c.isalpha() else "#"


def series_feed(page: int, path=None, letter: str | None = None) -> str:
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, publisher, book_count FROM shelf_series WHERE book_count > 0 ORDER BY title COLLATE NOCASE")]
    if letter:
        rows = [r for r in rows if _letter(r["title"]) == letter.upper()]
        rows.sort(key=lambda r: re.sub(r"^(the|a|an)\s+", "", r["title"].lstrip("-–— "), flags=re.I).lower())
    more = len(rows) > (page + 1) * PAGE
    rows = rows[page * PAGE:(page + 1) * PAGE]
    q = f"&letter={letter}" if letter else ""
    links = []
    if more:
        links.append(f'<link rel="next" href="/opds/series?page={page + 1}{q}" type={quoteattr(NAV)}/>')
    if page:
        links.append(f'<link rel="previous" href="/opds/series?page={page - 1}{q}" type={quoteattr(NAV)}/>')
    entries = [(f'<entry><id>urn:kometa:series:{s["id"]}</id><title>{escape(s["title"])}</title><updated>{_now()}</updated>'
                f'<link rel="subsection" href="/opds/series/{s["id"]}" type={quoteattr(ACQ)}/>'
                f'<link rel="http://opds-spec.org/image/thumbnail" href="/api/shelf/{s["id"]}/cover" type="image/jpeg"/>'
                f'<content type="text">{escape(s.get("publisher") or "")} · {s["book_count"]} books</content></entry>')
               for s in rows]
    return _feed(NAV, f"urn:kometa:opds:series:{letter or ''}:{page}", f"Series · {letter}" if letter else "All series",
                 links, entries, f"/opds/series?page={page}{q}")


def az_feed(path=None) -> str:
    path = path or DB_PATH
    with db._connect(path) as conn:
        titles = [r[0] for r in conn.execute("SELECT title FROM shelf_series WHERE book_count > 0")]
    counts: dict[str, int] = {}
    for t in titles:
        counts[_letter(t)] = counts.get(_letter(t), 0) + 1
    entries = [_nav_entry(f"urn:kometa:opds:az:{L}", L, f"/opds/series?letter={L}", f"{counts[L]} series", NAV)
               for L in sorted(counts, key=lambda x: (x == "#", x))]
    return _feed(NAV, "urn:kometa:opds:az", "Series by letter", [], entries, "/opds/az")


# --- reading lists and On Deck as feeds --------------------------------------------
def _books_by_ids(ids: list[int], path) -> list[dict]:
    if not ids:
        return []
    with db._connect(path) as conn:
        q = ",".join("?" * len(ids))
        rows = {r["id"]: dict(r) for r in conn.execute(f"""SELECT b.id, b.path, b.number, b.page_count, b.added_at,
            b.shelf_series_id, s.title AS series_title FROM books b LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
            WHERE b.id IN ({q})""", ids)}
    return [rows[i] for i in ids if i in rows]


def lists_feed(path=None) -> str:
    from kometa import readlists
    path = path or DB_PATH
    entries = []
    for l in readlists.get_lists(path):
        entries.append(_nav_entry(f"urn:kometa:list:{l['id']}", l["name"], f"/opds/lists/{l['id']}",
                                  f"{l.get('owned', 0)} of {l.get('total', l.get('entries', 0))} on the shelf"))
    return _feed(NAV, "urn:kometa:opds:lists", "Reading lists", [], entries, "/opds/lists")


def one_list_feed(list_id: int, path=None) -> str:
    """The list's owned books, in list order — what Panels can read right now."""
    from kometa import readlists
    path = path or DB_PATH
    try:
        res = readlists.resolve(list_id, path)
    except KeyError:
        raise HTTPException(404, "No such list")
    ids, seen = [], set()
    for e in res["entries"]:
        for b in e.get("books") or []:
            if b["id"] not in seen:
                seen.add(b["id"]); ids.append(b["id"])
    return _feed(ACQ, f"urn:kometa:list:{list_id}", res["name"], [], _entries(_books_by_ids(ids, path), path), f"/opds/lists/{list_id}")


def next_feed(path=None) -> str:
    from kometa.ondeck import up_next
    path = path or DB_PATH
    ids = [c["book_id"] for c in up_next(path=path)]
    return _feed(ACQ, "urn:kometa:opds:next", "Next", [], _entries(_books_by_ids(ids, path), path), "/opds/ondeck/next")


def favourites_feed(path=None) -> str:
    """Starred books, plus the first unread book of each starred series."""
    from kometa.marks import favourites
    path = path or DB_PATH
    ids = []
    with db._connect(path) as conn:
        for f in favourites(path):
            if f["kind"] == "book":
                ids.append(f["book_id"])
            else:
                r = conn.execute("""SELECT b.id FROM books b LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
                    WHERE b.tracked_series_id = ? AND COALESCE(p.completed, 0) = 0 AND b.number IS NOT NULL
                    ORDER BY b.number LIMIT 1""", (READER_ID, f["series_id"])).fetchone()
                if r:
                    ids.append(r["id"])
    ids = list(dict.fromkeys(ids))
    return _feed(ACQ, "urn:kometa:opds:favourites", "Favourites", [], _entries(_books_by_ids(ids, path), path), "/opds/ondeck/favourites")


# --- implicit progress: a page streamed is a page read ----------------------------
PREFETCH_LAG = 3


def note_page_read(book: dict, page0: int, path=None, reader_id=READER_ID) -> None:
    """Kavita's rule, with a lag: Panels fetches a few pages ahead of where you
    are (six in one burst, seen 2026-10-09), so the saved page trails the fetched
    one by PREFETCH_LAG. Never moves backwards, never un-finishes a book.
    Fetching the last page counts as finishing — a prefetch gets there only
    when you're already in the back matter."""
    path = path or DB_PATH
    count = book.get("page_count") or 0
    if count <= 0:
        return
    fetched = page0 + 1
    page = max(1, fetched - PREFETCH_LAG)
    done = fetched >= count
    if done:
        page = count
    cur = db.get_progress(reader_id, book["id"], path)
    if cur and (cur.get("completed") or (cur.get("page") or 0) >= page):
        return
    db.set_progress(reader_id, book["id"], page, done, _now(), path)


def one_series_feed(shelf_id: int, path=None) -> str:
    path = path or DB_PATH
    s = db.get_shelf_series(shelf_id, path)
    if not s:
        raise HTTPException(404, "No such series")
    books = _books_sql(path, "b.shelf_series_id = ?", (shelf_id,), "b.number IS NULL, b.number, b.path")
    return _feed(ACQ, f"urn:kometa:series:{shelf_id}", s["title"], [], _entries(books, path), f"/opds/series/{shelf_id}")


def keep_reading_feed(path=None) -> str:
    path = path or DB_PATH
    with db._connect(path) as conn:
        books = [dict(r) for r in conn.execute("""
            SELECT b.id, b.path, b.number, b.page_count, b.added_at, b.shelf_series_id, s.title AS series_title
            FROM read_progress p JOIN books b ON b.id = p.book_id LEFT JOIN shelf_series s ON s.id = b.shelf_series_id
            WHERE p.reader_id = ? AND p.completed = 0 ORDER BY p.updated_at DESC LIMIT 50""", (READER_ID,))]
    return _feed(ACQ, "urn:kometa:opds:keep-reading", "Keep reading", [], _entries(books, path), "/opds/keep-reading")


def recent_feed(path=None) -> str:
    path = path or DB_PATH
    books = _books_sql(path, "1=1", (), "b.added_at DESC, b.id DESC", 60)
    return _feed(ACQ, "urn:kometa:opds:recent", "Recently added", [], _entries(books, path), "/opds/recent")


def search_feed(q: str, path=None) -> str:
    path = path or DB_PATH
    q = (q or "").strip()
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, publisher, book_count FROM shelf_series WHERE book_count > 0 AND title LIKE ? ORDER BY title COLLATE NOCASE LIMIT 60",
            (f"%{q}%",))] if q else []
    entries = [(f'<entry><id>urn:kometa:series:{s["id"]}</id><title>{escape(s["title"])}</title><updated>{_now()}</updated>'
                f'<link rel="subsection" href="/opds/series/{s["id"]}" type={quoteattr(ACQ)}/>'
                f'<link rel="http://opds-spec.org/image/thumbnail" href="/api/shelf/{s["id"]}/cover" type="image/jpeg"/>'
                f'<content type="text">{escape(s.get("publisher") or "")} · {s["book_count"]} books</content></entry>')
               for s in rows]
    return _feed(NAV, "urn:kometa:opds:search", f"Search: {q}", [], entries, f"/opds/search?q={q}")


OPENSEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
<ShortName>Kometa</ShortName><Description>Search the shelf</Description>
<Url type="application/atom+xml;profile=opds-catalog;kind=navigation" template="/opds/search?q={searchTerms}"/>
</OpenSearchDescription>"""


# --- API --------------------------------------------------------------------------
def _xml(body: str, kind: str) -> Response:
    return Response(content=body, media_type=kind, headers={"Cache-Control": "no-cache"})


@router.get("/opds")
def api_root():
    return _xml(root_feed(), NAV)


@router.get("/opds/series")
def api_series(page: int = 0, letter: str | None = None):
    return _xml(series_feed(max(page, 0), letter=letter[:1] if letter else None), NAV)


@router.get("/opds/az")
def api_az():
    return _xml(az_feed(), NAV)


@router.get("/opds/lists")
def api_lists():
    return _xml(lists_feed(), NAV)


@router.get("/opds/lists/{list_id}")
def api_one_list(list_id: int):
    return _xml(one_list_feed(list_id), ACQ)


@router.get("/opds/ondeck/next")
def api_ondeck_next():
    return _xml(next_feed(), ACQ)


@router.get("/opds/ondeck/favourites")
def api_ondeck_favourites():
    return _xml(favourites_feed(), ACQ)


@router.get("/opds/series/{shelf_id}")
def api_one_series(shelf_id: int):
    return _xml(one_series_feed(shelf_id), ACQ)


@router.get("/opds/keep-reading")
def api_keep_reading():
    return _xml(keep_reading_feed(), ACQ)


@router.get("/opds/recent")
def api_recent():
    return _xml(recent_feed(), ACQ)


@router.get("/opds/search")
def api_search(q: str = ""):
    return _xml(search_feed(q), NAV)


@router.get("/opds/opensearch.xml")
def api_opensearch():
    return Response(content=OPENSEARCH, media_type="application/opensearchdescription+xml")


@router.api_route("/opds/books/{book_id}/file", methods=["GET", "HEAD"])
def api_file(book_id: int):
    b = db.get_book(book_id, DB_PATH)
    if not b or not os.path.exists(b["path"]):
        raise HTTPException(404, "No such book")
    media = MEDIA.get(os.path.splitext(b["path"])[1].lower(), "application/octet-stream")
    return FileResponse(b["path"], media_type=media, filename=os.path.basename(b["path"]))


@router.get("/opds/books/{book_id}/pages/{page}")
def api_page(book_id: int, page: int, maxWidth: int | None = None):
    """PSE page: 0-based, optional maxWidth → the reader's own buckets."""
    from kometa import reader
    book = reader._book_or_404(book_id)
    if not book.get("pages"):
        # first open: the reader's own scan fills the page list (and the real count)
        book = reader.ensure_book(book["path"], book.get("tracked_series_id"), book.get("number"))
    if not 0 <= page < (book["page_count"] or 0):
        raise HTTPException(404, "No such page")
    try:
        data = reader.get_page_bytes(book, page, reader._bucket(maxWidth))
    except FileNotFoundError:
        raise HTTPException(404, "File is gone from the shelf")
    except Exception as e:
        logger.warning(f"OPDS: page {page} of book {book_id} failed: {e}")
        raise HTTPException(422, "This page can't be rendered")
    try:
        note_page_read(book, page)
    except Exception as e:
        logger.info(f"OPDS: progress for book {book_id} not saved: {e}")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})
