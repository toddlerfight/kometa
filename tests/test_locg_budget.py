"""LOCG budget (2026-10-08): a sync only asks LOCG for what can have changed.
Finished runs monthly at most; ongoing runs at the caller's cadence; trades
monthly; anything you ask for (force) always fresh."""
from datetime import date, datetime, timedelta, timezone

import pytest

import kometa.db as db
import kometa.sync as sync


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def wired(db_path, series, monkeypatch):
    monkeypatch.setattr(sync, "DB_PATH", db_path)
    monkeypatch.setattr(sync, "_komga", lambda: None)
    calls = {"issues": 0, "trades": 0}
    def issues(sid):
        calls["issues"] += 1
        return [{"number": 1.0, "store_date": "2019-01-01", "cover": None, "locg_issue_id": "9"}]
    def trades(sid):
        calls["trades"] += 1
        return []
    monkeypatch.setattr(sync, "get_issues_anon", issues)
    monkeypatch.setattr(sync, "get_trades_anon", trades)
    db.set_locg_series_id(series, 123, db_path)
    return db_path, series, calls


def _sync(db_path, sid, **k):
    sync.sync_one(db.get_series_by_id(sid, db_path), **k)


def test_first_sync_fetches_and_stamps(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    assert calls == {"issues": 1, "trades": 1}
    assert db.get_series_by_id(sid, db_path)["locg_fetched_at"]


def test_finished_run_is_not_refetched_within_a_month(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    _sync(db_path, sid)
    _sync(db_path, sid)
    assert calls == {"issues": 1, "trades": 1}                  # newest issue is from 2019


def test_finished_run_refreshes_after_a_month(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    db.set_locg_fetched(sid, _ts(31), db_path)
    _sync(db_path, sid)
    assert calls["issues"] == 2


def test_ongoing_run_with_an_upcoming_issue_always_fetches(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    db.upsert_issue_status(sid, 2.0, str(date.today() + timedelta(days=20)), owned=False, path=db_path)
    _sync(db_path, sid)
    assert calls["issues"] == 2 and calls["trades"] == 1       # trades still monthly


def test_force_fetches_everything(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    _sync(db_path, sid, force=True)
    assert calls == {"issues": 2, "trades": 2}


def test_relinking_to_another_run_refetches(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    db.set_locg_series_id(sid, 456, db_path)
    _sync(db_path, sid)
    assert calls["issues"] == 2


def test_skipping_the_fetch_keeps_existing_issues(wired):
    db_path, sid, calls = wired
    _sync(db_path, sid)
    before = db.get_issues_for_series(sid, db_path)
    _sync(db_path, sid)
    assert [i["number"] for i in db.get_issues_for_series(sid, db_path)] == [i["number"] for i in before] == [1.0]
