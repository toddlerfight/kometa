"""Community ratings for a collected edition (the book sheet's Community line).

Issues carry theirs in community_json, merged in by each worker. A trade has no
record of its own to merge into, so it's read straight off the book sites'
tables by the trade's ISBN — Hardcover and Open Library, the two that rate
books. Same snapshot shape the issue sheet draws, so one renderer does both.
"""
import os

import kometa.db as db

DB_PATH = db.DB_PATH


def for_isbn(isbn: str | None, path=None) -> dict | None:
    """{hardcover, openlibrary} snapshots for an ISBN — only the sites that rated
    it. None when neither has (the sheet then shows no Community line)."""
    if not isbn:
        return None
    from kometa import hardcover, openlibrary
    path = path or DB_PATH
    out = {}
    for name, mod in (("hardcover", hardcover), ("openlibrary", openlibrary)):
        try:
            row = mod.get(isbn, path)
        except Exception:
            row = None
        if row and row.get("state") == "found" and row.get("rating") is not None and (row.get("rating_count") or 0) > 0:
            out[name] = mod.snapshot(row)
    return out or None


def trade_isbn(key: str, path=None) -> str | None:
    """The ISBN the catalogue gave this edition (trade_record), if any."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT isbn FROM trade_record WHERE key = ? AND COALESCE(isbn, '') != '' LIMIT 1",
                         (str(key),)).fetchone()
    return r[0] if r else None


def for_trade(key: str, path=None) -> dict | None:
    return for_isbn(trade_isbn(key, path), path)


def for_book(book_id: int, path=None) -> dict | None:
    """A shelf book that IS one of its run's trades (the trades list knows the
    file): that edition's ratings. Anything else — an issue, an untracked book,
    a trade the catalogue never listed — has none."""
    path = path or DB_PATH
    book = db.get_book(book_id, path)
    if not book or not book.get("tracked_series_id") or not book.get("path"):
        return None
    cached = db.get_trades(book["tracked_series_id"], path)
    name = os.path.basename(book["path"])
    for t in (cached or {}).get("trades") or []:
        if t.get("file") and os.path.basename(t["file"]) == name and t.get("locg_id"):
            return for_trade(t["locg_id"], path)
    return None
