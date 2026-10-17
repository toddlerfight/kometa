"""A 'done' row whose issue isn't on the shelf can be searched again (Buffy the Vampire Slayer
#1–#8, 2026-10-18: the wrong files went to the trash, the rows stayed 'done', and neither the
sweep nor Download could ask again). A real done — or one that only just landed — is left alone."""
import kometa.db as db


def _state(path, sid, n):
    with db._connect(path) as conn:
        return conn.execute("SELECT state FROM download_queue WHERE tracked_series_id = ? AND issue_number = ?", (sid, n)).fetchone()[0]


def test_a_done_row_for_an_issue_not_on_the_shelf_is_searched_again(db_path):
    sid = db.add_series(title="Buffy the Vampire Slayer", publisher="Dark Horse Comics", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 2.0, "1998-10-28", 0, path=db_path)       # missing (the wrong file was trashed)
    db.upsert_issue_status(sid, 9.0, "1999-05-26", 1, path=db_path)       # on the shelf
    db.upsert_issue_status(sid, 3.0, "1998-11-25", 0, path=db_path)       # landed a minute ago, not scanned yet
    with db._connect(db_path) as conn:
        for n, age in ((2.0, "-1 day"), (9.0, "-1 day"), (3.0, "-1 minute")):
            conn.execute("INSERT INTO download_queue (tracked_series_id, issue_number, state, updated_at) "
                         "VALUES (?, ?, 'done', datetime('now', ?))", (sid, n, age))
    for n in (2.0, 9.0, 3.0):
        db.queue_issue(sid, n, db_path)
    assert _state(db_path, sid, 2.0) == "queued"
    assert _state(db_path, sid, 9.0) == "done"                            # really on the shelf: left alone
    assert _state(db_path, sid, 3.0) == "done"                            # just landed: not fetched twice
    db.queue_issues_bulk([(sid, 2.0)], db_path)                           # the bulk path agrees
    assert _state(db_path, sid, 2.0) == "queued"
