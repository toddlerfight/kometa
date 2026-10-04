"""Ignored issues — born 2026-10-05 for Ripcord #0, a preview that never got a
real release and failed every sweep forever. Ignored = not owned, never missing:
no sweep, no search-missing, no count. And sync must not quietly un-ignore it."""
import sqlite3

import pytest
from fastapi import HTTPException

import kometa.db as db
import kometa.main as main
from kometa.main import IgnoreRequest

PAST = "2026-01-29"


def _seed(db_path, series):
    db.upsert_issue_status(series, 0.0, PAST, owned=False, path=db_path)
    db.upsert_issue_status(series, 1.0, PAST, owned=False, path=db_path)


def _missing(db_path, series):
    return db.get_all_series_summaries(db_path)[series]["missing"]


class TestDb:
    def test_migration_adds_column_defaulting_to_not_ignored(self, db_path, series):
        _seed(db_path, series)
        assert all(i["ignored"] == 0 for i in db.get_issues_for_series(series, db_path))

    def test_migration_is_idempotent_on_an_existing_db(self, db_path):
        db.init_db(db_path)   # second run must not blow up on the existing column
        cols = [r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(issue_status)")]
        assert cols.count("ignored") == 1

    def test_ignored_issue_drops_out_of_every_missing_view(self, db_path, series):
        _seed(db_path, series)
        assert _missing(db_path, series) == 2
        assert db.set_issue_ignored(series, 0.0, True, db_path)

        assert _missing(db_path, series) == 1
        assert db.get_missing_counts_by_series(db_path) == {series: 1}
        assert [r["number"] for r in db.get_missing_for_monitored(db_path)] == [1.0]

    def test_sync_upserts_do_not_unignore(self, db_path, series):
        _seed(db_path, series)
        db.set_issue_ignored(series, 0.0, True, db_path)
        # both upsert shapes sync uses
        db.upsert_issue_status_many([(series, 0.0, PAST, False, None, None, "123")], db_path)
        db.upsert_issue_status_bulk([(series, 0.0, False, None)], db_path)
        i0 = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 0.0)
        assert i0["ignored"] == 1

    def test_unignore_puts_it_back(self, db_path, series):
        _seed(db_path, series)
        db.set_issue_ignored(series, 0.0, True, db_path)
        db.set_issue_ignored(series, 0.0, False, db_path)
        assert _missing(db_path, series) == 2

    def test_unknown_issue_reports_false(self, db_path, series):
        assert db.set_issue_ignored(series, 99.0, True, db_path) is False


class TestEndpoint:
    @pytest.fixture
    def wired(self, db_path, series, monkeypatch):
        monkeypatch.setattr(main, "DB_PATH", db_path)
        monkeypatch.setattr(main, "_process_queue", lambda: None)
        _seed(db_path, series)
        return db_path, series

    def test_ignore_clears_a_parked_not_found_row(self, wired):
        db_path, series = wired
        db.queue_issue(series, 0.0, db_path)
        qid = next(q["id"] for q in db.get_queue(db_path) if q["issue_number"] == 0.0)
        db.update_queue_state(qid, "not_found", path=db_path)

        main.set_issue_ignored(series, 0.0, IgnoreRequest(ignored=True))

        assert not [q for q in db.get_queue(db_path) if q["issue_number"] == 0.0]

    def test_ignore_leaves_an_in_flight_download_alone(self, wired):
        db_path, series = wired
        db.queue_issue(series, 0.0, db_path)
        qid = next(q["id"] for q in db.get_queue(db_path) if q["issue_number"] == 0.0)
        db.update_queue_state(qid, "downloading", path=db_path)

        main.set_issue_ignored(series, 0.0, IgnoreRequest(ignored=True))

        assert [q["state"] for q in db.get_queue(db_path) if q["issue_number"] == 0.0] == ["downloading"]

    def test_search_missing_skips_ignored(self, wired):
        db_path, series = wired
        main.set_issue_ignored(series, 0.0, IgnoreRequest(ignored=True))

        assert main.search_missing(series) == {"queued": 1}
        assert [q["issue_number"] for q in db.get_queue(db_path)] == [1.0]

    def test_unknown_issue_404s(self, wired):
        _, series = wired
        with pytest.raises(HTTPException) as e:
            main.set_issue_ignored(series, 42.0, IgnoreRequest(ignored=True))
        assert e.value.status_code == 404
