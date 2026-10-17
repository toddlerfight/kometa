"""Page 1 is opening a book, not reading it (the owner, 2026-10-18): a place at page 1 isn't on
Continue reading, the OPDS keep-reading feed, or the 'in progress' counts. The apps saved page 1
on open; the web never did. Past page 1, it counts — per reader."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from test_ondeck import lib  # noqa: F401  (the On Deck library fixture)

import kometa.db as db
import kometa.ondeck as od


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_book_opened_at_page_1_is_not_continue_reading(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 1, False, _now(), lib["db"])         # opened, not read
    db.set_progress("me", b[("low", 1)], 2, False, _now(), lib["db"])          # turned a page: reading
    db.set_progress("u-tester", b[("saga", 2)], 7, False, _now(), lib["db"])   # someone else's place
    assert [(r["series"], r["label"]) for r in od.continue_reading(path=lib["db"])] == [("Low", "#1")]
    assert [r["label"] for r in od.continue_reading(reader_id="u-tester", path=lib["db"])] == ["#2"]


def test_the_opds_feed_and_the_counts_follow_the_same_rule(lib, monkeypatch):
    from kometa import opds
    monkeypatch.setattr(opds, "DB_PATH", lib["db"])
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 1, False, _now(), lib["db"])
    db.set_progress("me", b[("saga", 2)], 4, False, _now(), lib["db"])
    db.set_progress("me", b[("low", 1)], 6, False, _now(), lib["db"])
    db.set_dismissed("me", b[("low", 1)], True, lib["db"])                      # hidden with 'Not now'
    feed = opds.keep_reading_feed(lib["db"])
    assert feed.count("<entry>") == 1 and 'pse:lastRead="4"' in feed            # saga #2 only: not page 1, not hidden
    with db._connect(lib["db"]) as conn:
        n = conn.execute(db._SHELF_STATS_SQL, ("me",)).fetchall()
    in_progress = {r["sid"]: r["in_progress"] for r in n}
    assert sum(in_progress.values()) == 2                                       # saga #2 and low #1; not saga #1
