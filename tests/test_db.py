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
        assert issue["owned"] == 1
        assert issue["store_date"] == "2012-03-14"

        # folder_path stamped on the series in the same transaction
        assert db.get_series_by_id(series, db_path)["folder_path"] == "/comics/Image/Saga"

    def test_completed_issue_is_no_longer_missing(self, db_path, series):
        """The whole point of the atomic write: once done, no ghost re-download."""
        db.upsert_issue_status(series, 1.0, "2012-03-14", owned=False, path=db_path)

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


class TestEnvConfigSeeding:
    """Env vars provision config on first boot (so compose-only setup works),
    but never override what's already in the DB."""

    def test_komga_env_seeded_on_fresh_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOMGA_URL", "http://example:8585")
        monkeypatch.setenv("KOMGA_USER", "kuser")
        p = str(tmp_path / "k.db")
        db.init_db(p)
        cfg = db.get_config(p)
        assert cfg["komga_url"] == "http://example:8585"
        assert cfg["komga_user"] == "kuser"

    def test_unset_env_seeds_nothing(self, tmp_path, monkeypatch):
        for v in ("KOMGA_URL", "KOMGA_USER", "CV_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        p = str(tmp_path / "k.db")
        db.init_db(p)
        cfg = db.get_config(p)
        assert "komga_url" not in cfg
        assert "komga_user" not in cfg

    def test_env_does_not_override_existing_ui_value(self, tmp_path, monkeypatch):
        p = str(tmp_path / "k.db")
        monkeypatch.delenv("KOMGA_URL", raising=False)
        db.init_db(p)
        db.set_config({"komga_url": "http://ui-set:8585"}, p)  # user configured via UI

        monkeypatch.setenv("KOMGA_URL", "http://env-changed:9999")
        db.init_db(p)  # restart with a different env value

        assert db.get_config(p)["komga_url"] == "http://ui-set:8585"  # UI wins


class TestOwnedColumnMigration:
    def test_in_komga_renamed_to_owned_preserving_data(self, tmp_path):
        """A pre-rename DB (issue_status.in_komga) must migrate to .owned with
        every row's value intact, and the migration must be idempotent."""
        p = str(tmp_path / "old.db")
        conn = sqlite3.connect(p)
        conn.executescript("""
            CREATE TABLE issue_status (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_series_id INTEGER NOT NULL,
                number            REAL NOT NULL,
                store_date        TEXT,
                in_komga          INTEGER NOT NULL DEFAULT 0,
                komga_book_id     TEXT,
                UNIQUE(tracked_series_id, number)
            );
            INSERT INTO issue_status (tracked_series_id, number, store_date, in_komga)
                VALUES (1, 1.0, '2012-03-14', 1), (1, 2.0, NULL, 0);
        """)
        conn.commit()
        conn.close()

        db.init_db(p)  # runs the rename migration

        cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(issue_status)")}
        assert "owned" in cols and "in_komga" not in cols
        rows = {r["number"]: r["owned"] for r in db.get_issues_for_series(1, p)}
        assert rows == {1.0: 1, 2.0: 0}  # values carried through the rename

        db.init_db(p)  # idempotent — a second run must not error or double-rename
        rows2 = {r["number"]: r["owned"] for r in db.get_issues_for_series(1, p)}
        assert rows2 == {1.0: 1, 2.0: 0}


class TestFreshInstallMigration:
    def test_nullable_rebuild_keeps_locg_column(self, tmp_path):
        """Regression: the komga_series_id->nullable rebuild used to drop
        locg_series_id, crashing fresh installs on first write. (cv_volume_id is
        a vestigial column kept after the Comic Vine removal — no setter.)"""
        p = str(tmp_path / "fresh.db")
        db.init_db(p)

        cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(tracked_series)")}
        assert "locg_series_id" in cols
        assert "cv_volume_id" in cols  # still present, just unused now

        sid = db.add_series(komga_series_id=None, metron_series_id=None,
                            title="X", publisher="Y", path=p)
        db.set_locg_series_id(sid, 9999, p)
        row = db.get_series_by_id(sid, p)
        assert row["locg_series_id"] == 9999
