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


# --- the request: wanting what isn't here gets it (2026-10-24) ------------------------------
def _queued(dbp, sid):
    with db._connect(dbp) as c:
        return sorted(r[0] for r in c.execute("SELECT issue_number FROM download_queue WHERE tracked_series_id = ?", (sid,)))


def test_a_wanted_issue_that_is_out_is_queued_and_one_that_isnt_waits_for_release_day(want):
    dbp, ran = want["db"], []
    assert wr.fetch(want["low"], 3, path=dbp, process=lambda: ran.append(1), today="2026-10-24") == 1
    assert _queued(dbp, want["low"]) == [3.0] and ran == [1]
    db.set_pull_list(want["saga"], False, dbp)                        # Saga #4 is 2099's, and Saga's not pulled
    wr.add(series_id=want["saga"], number=4, path=dbp)
    assert wr.fetch(want["saga"], 4, path=dbp, process=lambda: None, today="2026-10-24") == 0
    with db._connect(dbp) as c:                                         # release day comes
        c.execute("UPDATE issue_status SET store_date = '2026-01-01' WHERE tracked_series_id = ? AND number = 4", (want["saga"],))
    assert [(m["tracked_series_id"], m["number"]) for m in db.get_missing_for_monitored(dbp)] == [(want["saga"], 4.0)]


def test_a_wanted_run_follows_the_pull_rule(want):
    dbp = want["db"]                                                    # Low: off the pull list, #3 missing
    assert wr.fetch(want["low"], path=dbp, process=lambda: None, today="2026-10-24") == 1
    assert db.get_series_by_id(want["low"], dbp)["on_pull_list"] == 1   # 33 or fewer: followed from now on
    assert wr.fetch(want["saga"], path=dbp, process=lambda: None) == 0  # already followed: the sweep has it


def test_a_run_kometa_doesnt_track_is_tracked_then_just_the_issue_comes(want, tmp_path):
    dbp = want["db"]
    s, created = wr.run_for(metron_series_id=777, title="Monstress (2015)", year=2015, publisher="Image",
                            path=dbp, root=str(tmp_path / "comics"))
    assert created and s["title"] == "Monstress" and s["on_pull_list"] == 0
    assert wr.run_for(metron_series_id=777, path=dbp)[0]["id"] == s["id"]          # twice is one run

    def synced(_s):                                                     # the catalogue's issues land
        db.upsert_issue_rows([(s["id"], float(n), "2016-01-01", False, None, None) for n in range(1, 6)], path=dbp)
    assert wr.fetch(s["id"], 2, created=True, path=dbp, sync=synced, process=lambda: None, today="2026-10-24") == 1
    assert _queued(dbp, s["id"]) == [2.0]
    ignored = {i["number"] for i in db.get_issues_for_series(s["id"], dbp) if i.get("ignored")}
    assert ignored == {1.0, 3.0, 4.0, 5.0}                              # 1 of 1, not 1 of 5


def test_taking_it_off_the_list_before_it_came_cancels_the_wait(want):
    dbp = want["db"]
    w = wr.add(series_id=want["low"], number=3, path=dbp)
    wr.fetch(want["low"], 3, path=dbp, process=lambda: None, today="2026-10-24")
    wr.add(series_id=want["low"], number=3, path=dbp, reader_id="pal")              # someone else wants it too
    wr.remove(w["id"], path=dbp)
    assert _queued(dbp, want["low"]) == [3.0]                                         # still theirs
    wr.remove(wr.find(series_id=want["low"], number=3, path=dbp, reader_id="pal")["id"], path=dbp, reader_id="pal")
    assert _queued(dbp, want["low"]) == []                                            # nobody wants it now


def test_whoever_wanted_it_hears_when_it_lands(want):
    from kometa import push
    wr.add(series_id=want["low"], number=3, path=want["db"], reader_id="pal")
    assert want["low"] in push.interested("pal", want["db"])
