"""Library calendar sort — 2026-10-07: Midnight Spider-Man #1 downloaded on release
day and its series dropped from today's slot to the bottom of the grid, because the
sort key (next_release) skips owned issues. calendar_date keeps this week's release."""
from datetime import date, timedelta

import kometa.db as db
import kometa.main as main

TODAY = date.today()
d = lambda n: str(TODAY + timedelta(days=n))   # noqa: E731


def _cal(db_path, sid):
    card = db.get_all_series_summaries(db_path)[sid]["calendar_date"]
    page = main._summary(db.get_issues_for_series(sid, db_path))["calendar_date"]
    assert card == page, "library card and series page must agree"
    return card


def _series(db_path, title):
    return db.add_series(komga_series_id=None, title=title, publisher="Marvel",
                         on_pull_list=True, path=db_path)


def test_release_downloaded_today_keeps_todays_slot(db_path):
    sid = _series(db_path, "Midnight Spider-Man")
    db.upsert_issue_status(sid, 1.0, d(0), owned=True, path=db_path)
    db.upsert_issue_status(sid, 2.0, d(42), owned=False, path=db_path)
    assert _cal(db_path, sid) == d(0)
    assert db.get_all_series_summaries(db_path)[sid]["next_release"] is None   # the old key, for the record


def test_this_weeks_release_holds_for_six_days_then_lets_go(db_path):
    held = _series(db_path, "Held")
    db.upsert_issue_status(held, 1.0, d(-6), owned=True, path=db_path)
    gone = _series(db_path, "Gone")
    db.upsert_issue_status(gone, 1.0, d(-7), owned=True, path=db_path)
    db.upsert_issue_status(gone, 2.0, d(7), owned=False, path=db_path)
    assert _cal(db_path, held) == d(-6)
    assert _cal(db_path, gone) == d(7)      # last week's is done — back to the next one


def test_no_recent_release_falls_back_to_next_upcoming(db_path):
    sid = _series(db_path, "Quiet")
    db.upsert_issue_status(sid, 1.0, d(-60), owned=True, path=db_path)
    db.upsert_issue_status(sid, 2.0, d(10), owned=False, path=db_path)
    assert _cal(db_path, sid) == d(10)


def test_ignored_issue_never_drives_the_calendar(db_path):
    sid = _series(db_path, "Ripcord")
    db.upsert_issue_status(sid, 0.0, d(-2), owned=False, path=db_path)
    db.set_issue_ignored(sid, 0.0, True, db_path)
    assert _cal(db_path, sid) is None


def test_sorts_ahead_of_next_weeks_books(db_path):
    today_owned = _series(db_path, "Down today")
    db.upsert_issue_status(today_owned, 1.0, d(0), owned=True, path=db_path)
    next_week = _series(db_path, "Next week")
    db.upsert_issue_status(next_week, 1.0, d(7), owned=False, path=db_path)
    s = db.get_all_series_summaries(db_path)
    assert s[today_owned]["calendar_date"] < s[next_week]["calendar_date"]


def test_library_card_counts_release_day_issue_without_changing_anything_else(db_path):
    # 2026-10-07, Walking Dead Deluxe read 145/145 with #146 out today. The card
    # gets out_today; missing/upcoming keep their meaning everywhere else.
    sid = _series(db_path, "The Walking Dead Deluxe")
    db.upsert_issue_status(sid, 145.0, d(-21), owned=True, path=db_path)
    db.upsert_issue_status(sid, 146.0, d(0), owned=False, path=db_path)
    db.upsert_issue_status(sid, 147.0, d(14), owned=False, path=db_path)
    card = db.get_all_series_summaries(db_path)[sid]
    assert (card["owned"], card["missing"], card["out_today"], card["upcoming"]) == (1, 0, 1, 2)
    db.upsert_issue_status(sid, 146.0, d(0), owned=True, path=db_path)
    assert db.get_all_series_summaries(db_path)[sid]["out_today"] == 0
