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


def test_a_page_turn_moves_progress_but_not_done(db_path, monkeypatch):
    """Each page watches its own stamps: a place kept mid-book is On Deck's business, a book
    finished is the series page's and the Library's."""
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        bid = c.execute("INSERT INTO books (path, tracked_series_id, number, page_count) VALUES ('/c/Saga 001.cbz', ?, 1, 30)", (sid,)).lastrowid
    a = main.changes()
    db.set_progress("me", bid, 7, False, "2026-10-29T10:00:00Z", db_path)          # a page turn
    b = main.changes()
    assert b["progress"] != a["progress"] and b["done"] == a["done"] and b["queue"] == a["queue"]
    db.set_progress("me", bid, 30, True, "2026-10-29T10:30:00Z", db_path)          # finished
    c2 = main.changes()
    assert c2["done"] != b["done"]
