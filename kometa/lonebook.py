"""A run that is one book.

LOCG files some original graphic novels as a series whose only entry is the
collected edition. Bedford Street (Fantagraphics, 2026): one 'TP', not a single
issue in sight. Kometa built zero issues, filed the book under Trades, and the run
never read as a run — no #1, and On Deck labelled it with the raw filename (the
owner, 2026-11-06: "It's only showing as a TRADE not as a series (even though
it's only a 1 off)").

The rule: exactly ONE edition (variant printings aside) with no volume number,
and no catalogue issue held. That edition IS issue #1. Its file is named
'<Title> #001 (<year>)' — the name every other part of Kometa already reads —
so rescan_owned makes #1 owned from disk, the reader and On Deck call it #1, and
the Trades tab stops listing it. A TPB plus an HC (Batman: Bad Seeds) is not one
book by this rule and stays a trade run; that's on purpose (the owner's call).

A file is only renamed after every linked catalogue has been asked AGAIN and
ANSWERED with no singles. A fetch that failed never passes for 'none' — a
renamed file is the one mistake here that outlives a re-sync.
"""
import logging
import os

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH


def _editions(series_id: int, path) -> list[dict]:
    cached = db.get_trades(series_id, path)
    return [t for t in ((cached or {}).get("trades") or []) if not t.get("is_variant")]


def edition(series: dict, path=None) -> dict | None:
    """The one edition this run is, from what's held (no network) — or None."""
    path = path or DB_PATH
    if not series or series.get("kind") == "arc":
        return None
    eds = _editions(series["id"], path)
    if len(eds) != 1:
        return None
    ed = eds[0]
    if ed.get("vol") is not None or ed.get("vol_range"):
        return None
    with db._connect(path) as conn:
        rows = conn.execute("SELECT number, locg_issue_id, metron_issue_id FROM issue_status "
                            "WHERE tracked_series_id = ?", (series["id"],)).fetchall()
    for r in rows:
        # the only issue a lone book may hold is the #1 it made of its own edition
        if r["number"] != 1 or r["metron_issue_id"] or (
                r["locg_issue_id"] and str(r["locg_issue_id"]) != str(ed.get("locg_id"))):
            return None
    return ed


def adopted(series: dict, path=None) -> dict | None:
    """The edition, once the run has taken it as an owned #1 — the Trades tab hides it."""
    path = path or DB_PATH
    ed = edition(series, path)
    if not ed:
        return None
    with db._connect(path) as conn:
        r = conn.execute("SELECT owned FROM issue_status WHERE tracked_series_id = ? AND number = 1",
                         (series["id"],)).fetchone()
    return ed if r and r["owned"] else None


def catalogue_says_none(series: dict) -> bool:
    """Ask again, strictly. True only when every linked catalogue ANSWERED and
    listed no singles; unlinked, refused, timed out or walled = False."""
    locg_id, mid = series.get("locg_series_id"), series.get("metron_series_id")
    if not locg_id and not mid:
        return False
    try:
        if locg_id:
            from kometa import locg_client
            if locg_client.get_issues_strict(locg_id):
                return False
        if mid:
            from kometa import metron_client
            if not metron_client.configured():
                return False
            for run in db.metron_series_ids(series):
                if metron_client.series_issues(run):
                    return False
    except Exception as e:
        logger.info(f"Lone book: {series.get('title')!r} — the catalogue didn't answer ({e}); no rename")
        return False
    return True


def issue_name(series: dict, ext: str) -> str:
    from kometa.naming import _safe
    year = series.get("year_began")
    return f"{_safe(series['title'])} #001{f' ({year})' if year else ''}{ext}"


def _record_date(series_id: int, ed: dict, path) -> str | None:
    try:
        with db._connect(path) as conn:
            r = conn.execute("SELECT store_date FROM trade_record WHERE tracked_series_id = ? AND locg_id = ?",
                             (series_id, str(ed.get("locg_id")))).fetchone()
        return r["store_date"] if r else None
    except Exception:
        return None


def adopt(series_id: int, files: list[str], path=None, check=None) -> str | None:
    """The edition's file becomes the run's #1: renamed to the issue name, its book
    row following (read progress rides on the book id), #1 stamped with the
    edition's LOCG page, cover and date. Returns the new path, or None — not a
    lone book, not exactly one file, #1's name already taken, or the catalogue
    didn't confirm. check: the strict catalogue ask (tests swap it)."""
    path = path or DB_PATH
    s = db.get_series_by_id(series_id, path)
    ed = edition(s, path) if s else None
    if not ed:
        return None
    readable = [f for f in files or [] if os.path.isfile(f)]
    if len(readable) != 1:
        return None
    src = readable[0]
    dst = os.path.join(os.path.dirname(src), issue_name(s, os.path.splitext(src)[1].lower()))
    if src != dst:
        if os.path.exists(dst):
            logger.info(f"Lone book: {s['title']!r} already has {os.path.basename(dst)} — left as is")
            return None
        if not (check or catalogue_says_none)(s):
            return None
        os.rename(src, dst)
        db.rename_book_path(src, dst, path)
        logger.info(f"Lone book: {s['title']!r} is one book — {os.path.basename(src)} → {os.path.basename(dst)}")
    db.upsert_issue_rows([(series_id, 1.0, _record_date(series_id, ed, path), True,
                           ed.get("cover"), ed.get("locg_id"))], path=path)
    return dst


def adopt_on_disk(series_id: int, path=None) -> str | None:
    """A lone book already on the shelf (filed as a trade before this rule)."""
    path = path or DB_PATH
    s = db.get_series_by_id(series_id, path)
    ed = edition(s, path) if s else None
    folder = (s or {}).get("folder_path")
    if not ed or not folder or not ed.get("file"):
        return None
    return adopt(series_id, [os.path.join(folder, ed["file"])], path)


if __name__ == "__main__":   # python -m kometa.lonebook <series_id> […]
    import sys
    logging.basicConfig(level=logging.INFO)
    for sid in map(int, sys.argv[1:]):
        new = adopt_on_disk(sid)
        if new:
            from kometa import shelf, sync
            shelf.index_series_folder(sid)
            sync.refresh_trades_owned(sid)
        print(sid, new or "not adopted")
