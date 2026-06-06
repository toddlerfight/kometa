"""db.py — the stuff that bites if it's wrong: the atomic complete_download
(the double-download fix lives or dies here), queue requeue rules, and the
fresh-install migration that once dropped columns on its way to nullable.
"""
import sqlite3

import kometa.db as db


def _qid_for(db_path, series_id, number):
    return next(q["id"] for q in db.get_queue(db_path)
               if q["tracked_series_id"] == series_id and q["issue_number"] == number)


class TestCompleteDownload:
    def test_marks_done_and_owns_issue_in_one_shot(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)

        db.complete_download(
            qid, series, 1.0, store_date="2012-03-14",
            filename="/comics/Image/Saga/Saga #001.cbz",
            set_folder_path="/comics/Image/Saga", path=db_path,
        )

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        assert q["filename"] == "/comics/Image/Saga/Saga #001.cbz"

        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["in_komga"] == 1
        assert issue["store_date"] == "2012-03-14"

        # folder_path stamped on the series in the same transaction
        assert db.get_series_by_id(series, db_path)["folder_path"] == "/comics/Image/Saga"

    def test_completed_issue_is_no_longer_missing(self, db_path, series):
        """The whole point of the atomic write: once done, no ghost re-download."""
        db.upsert_issue_status(series, 1.0, "2012-03-14", in_komga=False, path=db_path)

        # missing before we touch it (no live queue row yet)
        before = {r["number"] for r in db.get_missing_for_monitored(db_path)}
        assert 1.0 in before

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.complete_download(qid, series, 1.0, "2012-03-14",
                             filename="/x/Saga #001.cbz", path=db_path)

        after = {r["number"] for r in db.get_missing_for_monitored(db_path)}
        assert 1.0 not in after

    def test_no_set_folder_path_leaves_series_untouched(self, db_path, series):
        db.queue_issue(series, 2.0, db_path)
        qid = _qid_for(db_path, series, 2.0)
        db.complete_download(qid, series, 2.0, None,
                             filename="/x/Saga #002.cbz", path=db_path)
        assert db.get_series_by_id(series, db_path)["folder_path"] is None


class TestQueueRequeue:
    def test_failed_item_requeues_and_clears_error(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "failed", error="boom", path=db_path)

        db.queue_issue(series, 1.0, db_path)  # re-request

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "queued"
        assert q["error"] is None

    def test_done_item_is_not_requeued(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "done", path=db_path)

        db.queue_issue(series, 1.0, db_path)  # should be a no-op on state

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"


class TestResetStuckQueueItems:
    def test_searching_and_downloading_reset_to_queued(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        db.queue_issue(series, 2.0, db_path)
        ids = {1.0: _qid_for(db_path, series, 1.0), 2.0: _qid_for(db_path, series, 2.0)}
        db.update_queue_state(ids[1.0], "searching", path=db_path)
        db.update_queue_state(ids[2.0], "downloading", path=db_path)

        db.reset_stuck_queue_items(db_path)

        states = {q["id"]: q["state"] for q in db.get_queue(db_path)}
        assert states[ids[1.0]] == "queued"
        assert states[ids[2.0]] == "queued"


class TestFreshInstallMigration:
    def test_nullable_rebuild_keeps_cv_and_locg_columns(self, tmp_path):
        """Regression: the komga_series_id->nullable rebuild used to drop
        cv_volume_id/locg_series_id, crashing fresh installs on first write."""
        p = str(tmp_path / "fresh.db")
        db.init_db(p)

        cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(tracked_series)")}
        assert "cv_volume_id" in cols
        assert "locg_series_id" in cols

        # and the setters that read/write them don't blow up
        sid = db.add_series(komga_series_id=None, metron_series_id=None,
                            title="X", publisher="Y", path=p)
        db.set_cv_volume_id(sid, "4050-1234", p)
        db.set_locg_series_id(sid, 9999, p)
        row = db.get_series_by_id(sid, p)
        assert row["cv_volume_id"] == "4050-1234"
        assert row["locg_series_id"] == 9999
