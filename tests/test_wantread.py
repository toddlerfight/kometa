"""Want to read (kometa/wantread.py): books, issues and whole runs you mean to
read, a row on On Deck in the order you added them, gone once finished."""
import pytest

import kometa.db as db
import kometa.wantread as wr
from tests.test_ondeck import lib  # noqa: F401  (Saga #1–3 + a TPB, Low #1–2; Saga #4 and Low #3 not owned)


@pytest.fixture
def want(lib, monkeypatch):  # noqa: F811
    monkeypatch.setattr(wr, "DB_PATH", lib["db"])
    return lib


def _row(lib):
    return [(c["kind"], c["series"], c["label"]) for c in wr.row(lib["db"])]


def test_an_issue_book_is_kept_as_the_issue_and_a_trade_as_the_book(want):
    b = want["b"]
    w1 = wr.add(book_id=b[("saga", 2)], path=want["db"])
    assert (w1["book_id"], w1["series_id"], w1["number"]) == (None, want["saga"], 2.0)    # survives a rename
    w2 = wr.add(book_id=b[("saga", "tpb")], path=want["db"])
    assert w2["book_id"] == b[("saga", "tpb")] and w2["series_id"] is None
    assert wr.add(book_id=b[("saga", 2)], path=want["db"])["id"] == w1["id"]              # adding twice is one entry
    assert wr.find(series_id=want["saga"], number=2, path=want["db"])["id"] == w1["id"]


def test_the_row_keeps_your_order_and_shows_what_isnt_here_yet(want):
    wr.add(series_id=want["saga"], number=4, path=want["db"])                             # not owned yet
    wr.add(book_id=want["b"][("low", 1)], path=want["db"])
    wr.add(series_id=want["low"], path=want["db"])                                         # a whole run
    rows = _row(want)
    assert rows[0] == ("gap", "Saga", "#4") and rows[1] == ("book", "Low", "#1") and rows[2] == ("book", "Low", "#1")
    assert all(c.get("want_id") for c in wr.row(want["db"]))


def test_a_finished_entry_leaves_and_a_run_moves_on(want):
    b, dbp = want["b"], want["db"]
    wr.add(book_id=b[("saga", 1)], path=dbp)
    wr.add(series_id=want["low"], path=dbp)
    db.set_progress("me", b[("saga", 1)], 3, False, "2026-10-01T10:00:00Z", dbp)          # started: still wanted
    assert ("book", "Saga", "#1") in _row(want)
    db.set_progress("me", b[("saga", 1)], 24, True, "2026-10-02T10:00:00Z", dbp)          # finished: gone
    db.set_progress("me", b[("low", 1)], 24, True, "2026-10-02T10:00:00Z", dbp)
    assert _row(want) == [("book", "Low", "#2")]                                           # the run's next
    db.set_progress("me", b[("low", 2)], 24, True, "2026-10-03T10:00:00Z", dbp)
    assert [(c["kind"], c["missing"]) for c in wr.row(dbp)] == [("series", 1)]             # all read, #3 still to come
    with db._connect(dbp) as c:
        c.execute("UPDATE issue_status SET ignored = 1 WHERE tracked_series_id = ? AND number = 3", (want["low"],))
    assert wr.row(dbp) == [] and wr.items(dbp) == []                                       # nothing left: off the list


def test_remove(want):
    w = wr.add(series_id=want["saga"], path=want["db"])
    wr.remove(w["id"], path=want["db"])
    assert wr.items(want["db"]) == []


def test_works_on_the_default_database(want):
    # the routes call these with no path: the default must reach every lookup
    w = wr.add(series_id=want["saga"], number=4)
    assert wr.find(series_id=want["saga"], number=4)["id"] == w["id"]
    assert wr.find(book_id=want["b"][("saga", 1)]) is None
