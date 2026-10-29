"""An edition of a one-book run carries that book's details.

Joker (2008) is one graphic novel; its 10th Anniversary and Deluxe Editions are the
same story in a different binding. Filed under the original they opened onto a bare
card — publisher, pages, nothing else — while #1, one row up, named the whole team
(the owner, 2026-10-30). So: a book with no issue number, in a run Metron says is a
single book, wears that book's description, credits and release date.

A trade of a longer run gets none of this. Black Science Vol. 2 collects #7–#11, not
all forty-three, and slapping the whole run's credits on it would be a guess dressed
up as a fact. Those read their catalogue trade entry, or stay bare. Stored record
only: opening a card never spends Metron's budget."""
import kometa.db as db
from kometa import record

DB_PATH = db.DB_PATH


def for_book(book_id: int, path=None) -> dict | None:
    """{desc, credits, store_date, of} for an edition of a one-book run, else None."""
    path = path or DB_PATH
    book = db.get_book(book_id, path)
    if not book or not book.get("tracked_series_id") or book.get("number") is not None:
        return None
    sid = book["tracked_series_id"]
    issues = db.get_issues_for_series(sid, path)
    if len(issues) != 1:
        return None
    issue = issues[0]
    d = record.details(issue, path, stored_only=True)
    if not (d.get("credits") or d.get("desc")):
        return None
    s = db.get_series_by_id(sid, path) or {}
    return {"desc": d.get("desc") or "", "credits": d.get("credits") or [],
            "store_date": issue.get("store_date") or d.get("store_date"),
            "of": f"{s.get('title') or ''} #{issue['number']:g}".strip()}
