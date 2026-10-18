"""The db sweep's four bugs (partner review, 2026-10-18), each pinned."""
import kometa.db as db


def _q(db_path, sid, n):
    with db._connect(db_path) as conn:
        return dict(conn.execute("SELECT * FROM download_queue WHERE tracked_series_id = ? AND issue_number = ?", (sid, n)).fetchone())


def _set(db_path, sql, *args):
    with db._connect(db_path) as conn:
        conn.execute(sql, args)


def test_a_download_keeps_the_issues_metron_cover(db_path, series):
    db.upsert_issue_status(series, 3.0, "2012-05-02", False, metron_image="https://metron/saga3.jpg", path=db_path)
    db.queue_issue(series, 3.0, db_path)
    qid = _q(db_path, series, 3.0)["id"]
    db.complete_download(qid, series, 3.0, "2012-05-02", "/comics/Saga/Saga #003.cbz", path=db_path)
    row = db.get_issues_for_series(series, db_path, number=3.0)[0]
    assert row["owned"] == 1 and row["metron_image"] == "https://metron/saga3.jpg"


def test_a_bulk_requeue_lifts_the_not_found_wait(db_path, series):
    db.upsert_issue_status(series, 11.0, "1999-07-01", False, path=db_path)
    db.queue_issue(series, 11.0, db_path)
    _set(db_path, "UPDATE download_queue SET state = 'not_found', retry_after = datetime('now', '+3 days') "
                  "WHERE tracked_series_id = ?", series)
    db.queue_issues_bulk([(series, 11.0)], db_path)
    q = _q(db_path, series, 11.0)
    assert q["state"] == "queued" and q["retry_after"] is None
    assert [i["issue_number"] for i in db.get_queued_items(db_path)] == [11.0]   # picked, not just shown


def test_a_trashed_done_issue_is_asked_again_by_the_nightly_sweep(db_path, series, tmp_path):
    db.set_series_pull_list(series, True, db_path) if hasattr(db, "set_series_pull_list") else \
        _set(db_path, "UPDATE tracked_series SET on_pull_list = 1 WHERE id = ?", series)
    kept = tmp_path / "Saga #002.cbz"
    kept.write_bytes(b"x")
    for n, f in ((1.0, str(tmp_path / "Saga #001.cbz")), (2.0, str(kept)), (4.0, str(tmp_path / "Saga #004.cbz"))):
        db.upsert_issue_status(series, n, "2012-03-14", False, path=db_path)
        db.queue_issue(series, n, db_path)
        _set(db_path, "UPDATE download_queue SET state = 'done', filename = ?, updated_at = datetime('now', '-1 hour') "
                      "WHERE tracked_series_id = ? AND issue_number = ?", f, series, n)
    _set(db_path, "UPDATE issue_status SET covered_by = '/comics/Saga/Saga Vol 1.cbz' WHERE tracked_series_id = ? AND number = 4", series)
    missing = {r["number"] for r in db.get_missing_for_monitored(db_path)}
    assert 1.0 in missing          # its file was trashed: ask again
    assert 2.0 not in missing      # the file is still there: an ownership slip, not a missing book
    assert 4.0 not in missing      # an owned trade has it


def test_a_requeued_pack_drops_the_other_channels_handle_and_the_old_error(db_path, series):
    db.queue_torrent_pack(series, "abc123", "magnet:?xt=old", db_path)
    _set(db_path, "UPDATE download_queue SET state = 'failed', error = 'stalled' WHERE tracked_series_id = ? AND issue_number = -1", series)
    db.queue_pack(series, "nzo-1", "http://nzb", db_path)
    q = _q(db_path, series, -1)
    assert q["state"] == "pending_usenet" and q["sab_nzo_id"] == "nzo-1"
    assert q["torrent_hash"] is None and q["error"] is None
    _set(db_path, "UPDATE download_queue SET state = 'failed' WHERE tracked_series_id = ? AND issue_number = -1", series)
    db.queue_torrent_pack(series, "def456", "magnet:?xt=new", db_path)
    q = _q(db_path, series, -1)
    assert q["torrent_hash"] == "def456" and q["sab_nzo_id"] is None
