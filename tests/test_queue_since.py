"""/api/queue?since= — Activity's poll asks for what changed, not the whole queue
(it fetched every row every 2 s while anything was moving)."""
import kometa.db as db
import kometa.main as main


def _rows(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    for n in (1.0, 2.0, 3.0):
        db.queue_issue(sid, n, db_path)
    q = {r["issue_number"]: r["id"] for r in db.get_queue(db_path)}
    with db._connect(db_path) as c:
        c.execute("UPDATE download_queue SET state = 'done', updated_at = '2026-10-01 00:00:00' WHERE id IN (?, ?)", (q[1.0], q[2.0]))
        c.execute("UPDATE download_queue SET state = 'downloading', updated_at = '2026-10-01 00:00:00' WHERE id = ?", (q[3.0],))
    return sid, q


def test_without_since_the_answer_is_the_old_list(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    _rows(db_path)
    out = main.get_queue()
    assert isinstance(out, list) and len(out) == 3


def test_since_sends_changes_and_live_rows_and_every_id_with_its_state(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid, q = _rows(db_path)
    full = main.get_queue(since="")
    assert {r["id"] for r in full["rows"]} == set(q.values()) and full["at"]
    assert "source_url" not in full["rows"][0]                                  # still scrubbed

    quiet = main.get_queue(since=full["at"])
    assert [r["id"] for r in quiet["rows"]] == [q[3.0]]                         # the download: its progress lives in memory
    assert sorted(quiet["states"]) == sorted([[q[1.0], "done"], [q[2.0], "done"], [q[3.0], "downloading"]])

    db.update_queue_state(q[1.0], "failed", error="bad CRC", path=db_path)
    with db._connect(db_path) as c:
        c.execute("DELETE FROM download_queue WHERE id = ?", (q[2.0],))
    moved = main.get_queue(since=quiet["at"])
    assert {r["id"] for r in moved["rows"]} == {q[1.0], q[3.0]}
    assert q[2.0] not in {i for i, _ in moved["states"]}                        # deleted: the page drops it
