"""Credits the ladder fills reach the file's ComicInfo now, not at the weekly recheck
(retag.requeue_issue_now, the owner 2026-10-18)."""
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from test_retag import _run, lib  # noqa: F401  (the fixture and the runner)

import kometa.db as db
import kometa.record as record
from kometa import retag


def test_ladder_credits_go_into_the_file_straight_away(lib):
    db.set_config({"retag_enabled": "1"}, lib["db"])
    record.write_issue(lib["series"], 1.0, {"desc": "x", "credits": [], "cover_date": "2012-03-01", "extra": {"gcd_id": 7}},
                       "metron", "full", path=lib["db"])
    with db._connect(lib["db"]) as conn:                  # an issue Metron has no credits for
        conn.execute("UPDATE issue_record SET credits_json = '[]' WHERE tracked_series_id = ?", (lib["series"],))
    retag.enqueue([lib["book_id"]], "run", lib["db"])
    run = lambda ids: _run(lib, ids)
    assert retag.retag_tick(path=lib["db"], runner=run)["counts"] == {"swapped": 1}
    # the ladder finds credits the same day the file was tagged: no week's wait
    team = [{"role": "Writer", "name": "Jhonen Vasquez", "metron_creator_id": None}]
    assert record.fill_credits(lib["series"], 1.0, lib["db"], rungs=(("gcd", lambda r: team),))["source"] == "gcd"
    assert retag.journal(lib["book_id"], lib["db"])["queued_for"] == "changed"
    assert retag.retag_tick(path=lib["db"], runner=run)["counts"] == {"swapped": 1}
    with zipfile.ZipFile(lib["file"]) as zf:
        assert b"Jhonen Vasquez" in zf.read("ComicInfo.xml")
    # nothing new to write: not queued again
    assert retag.requeue_issue_now(lib["series"], 1.0, lib["db"]) == 0
