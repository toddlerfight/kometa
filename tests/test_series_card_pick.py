"""The library card's cover issue (db.get_all_series_summaries), picked once per series:
an ignored issue is never the next-issue cover, and a same-date tie breaks on the number
with number and art read off the same issue (db sweep, 2026-10-18)."""
from datetime import date, timedelta

import kometa.db as db

TODAY = date.today()


def _d(days):
    return str(TODAY + timedelta(days=days))


def _issue(db_path, sid, n, days, owned=False, art=None, ignored=False):
    db.upsert_issue_status(sid, n, _d(days), owned, metron_image=art, path=db_path)
    if ignored:
        db.set_issue_ignored(sid, n, True, db_path)


def test_an_ignored_issue_is_never_the_next_issue_cover(db_path, series):
    _issue(db_path, series, 5.0, 3, art="https://metron/5.jpg", ignored=True)
    _issue(db_path, series, 6.0, 10, art="https://metron/6.jpg")
    card = db.get_all_series_summaries(db_path)[series]
    assert card["card_image"] == "https://metron/6.jpg"
    assert card["next_release"] == _d(10)                 # agrees with the counts, which skip it too


def test_a_same_date_tie_breaks_on_the_number_and_reads_one_issue(db_path, series):
    # two out the same past day, neither owned: the higher number is the newer card
    _issue(db_path, series, 1.0, -40, art="https://metron/1.jpg")
    _issue(db_path, series, 2.0, -40, art="https://metron/2.jpg")
    assert db.get_all_series_summaries(db_path)[series]["card_image"] == "https://metron/2.jpg"
    # two coming the same day: the lower number is the next one
    _issue(db_path, series, 4.0, 7, art="https://metron/4.jpg")
    _issue(db_path, series, 3.0, 7, art="https://metron/3.jpg")
    assert db.get_all_series_summaries(db_path)[series]["card_image"] == "https://metron/3.jpg"


def test_one_series_and_all_series_agree(db_path, series):
    _issue(db_path, series, 1.0, -40, owned=True, art="https://metron/1.jpg")
    _issue(db_path, series, 2.0, 12, art="https://metron/2.jpg")
    assert db.get_all_series_summaries(db_path, series_id=series)[series] == db.get_all_series_summaries(db_path)[series]
