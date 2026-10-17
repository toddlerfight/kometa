"""/api/changes: the stamp the shelf pages poll moves when what they show moves."""
import kometa.db as db
import kometa.main as main


def test_the_stamp_moves_with_the_shelf(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Buffy the Vampire Slayer", publisher="Dark Horse Comics", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "1998-09-23", 0, path=db_path)
    a = main.changes()["stamp"]
    assert main.changes()["stamp"] == a                                   # nothing moved: same stamp
    db.upsert_issue_status(sid, 1.0, "1998-09-23", 1, path=db_path)      # #1 lands
    b = main.changes()["stamp"]
    assert b != a
    with db._connect(db_path) as c:                                      # a search queued
        c.execute("INSERT INTO download_queue (tracked_series_id, issue_number, state) VALUES (?, 2, 'queued')", (sid,))
    assert main.changes()["stamp"] != b
