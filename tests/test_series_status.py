"""The light endpoints the UI polls (main.series_status, main.queue_summary)."""
import kometa.db as db
import kometa.main as main


def test_status_carries_what_the_pollers_compare(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-04-11", 0, path=db_path)
    st = main.series_status(sid)
    assert {k: st[k] for k in ("owned", "missing", "issue_count")} == {"owned": 1, "missing": 1, "issue_count": 2}
    assert "issues" not in st and "last_synced" in st


def test_badge_summary_counts_only_whats_new_since_you_looked(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    for n, state, at in ((1.0, "failed", "2026-10-11 05:00:00"), (2.0, "done", "2026-10-11 05:00:00"), (3.0, "done", "2026-10-10 01:00:00")):
        db.queue_issue(sid, n, db_path)
        with db._connect(db_path) as c:
            c.execute("UPDATE download_queue SET state = ?, updated_at = ? WHERE issue_number = ?", (state, at, n))
    assert main.queue_summary(since="2026-10-11 00:00:00") == {"failed": 1, "not_found": 0, "done": 1}
    assert main.queue_summary()["done"] == 2


def test_the_queue_doesnt_hand_out_indexer_urls(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.queue_issue(sid, 1.0, db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE download_queue SET source_url = 'http://indexer/?apikey=secret'")
    assert "source_url" not in main.get_queue()[0]
