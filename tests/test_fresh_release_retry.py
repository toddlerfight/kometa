"""A fresh release skips the not_found wait; Activity's next_try names the pass that
re-queues a row (Absolute Flash #20 & co., 2026-10-21: one search at 3:05pm, next
chance Saturday)."""
from datetime import date, datetime, timedelta, timezone

import pytest

import kometa.db as db
from kometa import scheduler
from kometa.scheduler import next_try


def _set(db_path, sql, *args):
    with db._connect(db_path) as conn:
        conn.execute(sql, args)


def _not_found(db_path, series, n, store_date):
    _set(db_path, "UPDATE tracked_series SET on_pull_list = 1 WHERE id = ?", series)
    db.upsert_issue_status(series, n, store_date, False, path=db_path)
    db.queue_issue(series, n, db_path)
    _set(db_path, "UPDATE download_queue SET state = 'not_found', retry_after = datetime('now', '+3 days') "
                  "WHERE tracked_series_id = ? AND issue_number = ?", series, n)


def test_a_fresh_release_that_wasnt_found_is_asked_again(db_path, series):
    _not_found(db_path, series, 20.0, str(date.today() - timedelta(days=1)))
    assert 20.0 in {r["number"] for r in db.get_missing_for_monitored(db_path)}


def test_an_old_issue_still_sits_out_its_wait(db_path, series):
    _not_found(db_path, series, 3.0, "2012-05-02")
    assert 3.0 not in {r["number"] for r in db.get_missing_for_monitored(db_path)}


@pytest.fixture
def nightly(monkeypatch):
    monkeypatch.setattr(scheduler, "sync_hours", lambda: [4])


# Wed 2026-10-21 17:00 Brisbane = 07:00 UTC
WED_5PM = datetime(2026, 10, 21, 7, 0, 30, tzinfo=timezone.utc)


def _row(**k):
    return {"state": "not_found", "kind": "issue", "issue_number": 20.0, "on_pull_list": 1,
            "ignored": 0, "store_date": "2026-10-21", "retry_after": "2026-10-24 05:05:26", **k}


def test_a_fresh_release_is_next_tried_at_the_next_release_retry(nightly):
    assert next_try(_row(), WED_5PM) == "2026-10-21 09:00:00"         # 7pm Brisbane


def test_an_old_issue_waits_for_the_first_nightly_sweep_after_its_wait(nightly):
    row = _row(store_date="2012-05-02")
    assert next_try(row, WED_5PM) == "2026-10-24 18:00:00"            # Sun 4am Brisbane


def test_a_release_falling_out_of_the_week_goes_back_to_its_wait(nightly):
    # out 2026-10-13: past the week by the 4am sweep (still the 21st in UTC); three days to sit out
    row = _row(store_date="2026-10-13", retry_after="2026-10-24 05:05:26")
    late = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)      # 11:30pm Brisbane
    assert next_try(row, late) == "2026-10-24 18:00:00"


def test_nothing_automatic_says_none(nightly):
    assert next_try(_row(state="failed"), WED_5PM) is None
    assert next_try(_row(on_pull_list=0), WED_5PM) is None
    assert next_try(_row(ignored=1), WED_5PM) is None
    assert next_try(_row(kind="trade"), WED_5PM) is None


def test_issue_zero_is_an_issue(nightly):
    assert next_try(_row(issue_number=0.0), WED_5PM) == "2026-10-21 09:00:00"


def test_a_rate_limit_park_is_its_own_wait(nightly):
    row = _row(state="queued", retry_after="2026-10-21 07:20:00")
    assert next_try(row, WED_5PM) == "2026-10-21 07:20:00"
    assert next_try(_row(state="queued", retry_after=None), WED_5PM) is None
