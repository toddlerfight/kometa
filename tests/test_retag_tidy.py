"""The retag journal tidies itself (retag.tidy_journal, run each tick). 2026-10-17: re-filing
The Joker Presents: A Puzzlebox gave its 14 files new book ids; the 17 rows under the old ids
sat 'pending' for a day and a half (the tick skips a row whose book is gone), and a volume that
changed mid-run sat 'failed' for three days though nothing was wrong with it."""
import os

import kometa.db as db
import kometa.retag as retag


def _book(db_path, tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    st = os.stat(p)
    return db.upsert_book(str(p), st.st_size, st.st_mtime, [], tracked_series_id=None, number=1.0, path=db_path)


def test_a_row_whose_book_is_gone_is_retired_not_waiting(db_path, tmp_path):
    bid = _book(db_path, tmp_path, "Puzzlebox #011.cbz")
    retag.enqueue([bid], "arrival", path=db_path)
    with db._connect(db_path) as c:
        c.execute("DELETE FROM books WHERE id = ?", (bid,))           # re-filed: a new id took over the file
    assert retag.tidy_journal(db_path)["retired"] == 1
    row = retag.journal(bid, db_path)
    assert row["state"] == "skipped" and "gone" in row["error"]
    assert retag.tidy_journal(db_path)["retired"] == 0                 # once


def test_a_file_that_changed_mid_run_goes_back_on_the_list_a_few_times(db_path, tmp_path):
    bid = _book(db_path, tmp_path, "SIKTC Vol 03.cbz")
    retag.enqueue([bid], "changed", path=db_path)
    retag._set(bid, db_path, state="failed", error=retag.CHANGED_MID_RUN + " (482079262 → 482078840 bytes)", attempts=1)
    assert retag.tidy_journal(db_path)["retried"] == 1
    assert retag.journal(bid, db_path)["state"] == "pending"
    retag._set(bid, db_path, state="failed", error=retag.CHANGED_MID_RUN, attempts=retag.RETRY_CHANGED)
    assert retag.tidy_journal(db_path)["retried"] == 0                 # one that keeps changing is left failed
    assert retag.journal(bid, db_path)["state"] == "failed"


def test_other_failures_are_left_for_a_person(db_path, tmp_path):
    bid = _book(db_path, tmp_path, "Broken #001.cbz")
    retag.enqueue([bid], "run", path=db_path)
    retag._set(bid, db_path, state="failed", error="BadZipFile: Bad CRC-32 for file '001.jpg'", attempts=1)
    assert retag.tidy_journal(db_path) == {"retired": 0, "retried": 0}
    assert retag.journal(bid, db_path)["state"] == "failed"
