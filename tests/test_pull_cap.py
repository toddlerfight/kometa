"""Track from a card = on the pull list — but one tap must never queue a 500-issue run
(kometa/main.py _auto_pull, PULL_AUTO_CAP)."""
import kometa.db as db
from kometa import main


def _run(db_path, missing: int, owned: int = 0):
    sid = db.add_series(title="Big Run", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    n = 1
    for _ in range(owned):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 1, path=db_path); n += 1
    for _ in range(missing):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 0, path=db_path); n += 1
    return sid


def _queued(db_path, sid):
    with db._connect(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM download_queue WHERE tracked_series_id = ?", (sid,)).fetchone()[0]


def test_under_the_cap_everything_missing_is_queued(db_path):
    sid = _run(db_path, missing=main.PULL_AUTO_CAP, owned=2)
    assert main._auto_pull(sid, db.get_issues_for_series(sid, db_path), today="2026-10-16", path=db_path) == main.PULL_AUTO_CAP
    assert _queued(db_path, sid) == main.PULL_AUTO_CAP
    assert db.get_series_by_id(sid, db_path)["on_pull_list"] == 1


def test_over_the_cap_queues_nothing_and_switches_the_pull_list_off(db_path):
    sid = _run(db_path, missing=main.PULL_AUTO_CAP + 1)
    assert main._auto_pull(sid, db.get_issues_for_series(sid, db_path), today="2026-10-16", path=db_path) == 0
    assert _queued(db_path, sid) == 0
    assert db.get_series_by_id(sid, db_path)["on_pull_list"] == 0


def test_unreleased_and_ignored_issues_do_not_count_toward_the_cap(db_path):
    sid = _run(db_path, missing=main.PULL_AUTO_CAP)
    with db._connect(db_path) as c:                       # 40 more that are solicited, not out
        for n in range(100, 140):
            c.execute("INSERT INTO issue_status (tracked_series_id, number, store_date, owned) VALUES (?, ?, '2027-01-01', 0)", (sid, float(n)))
    assert main._auto_pull(sid, db.get_issues_for_series(sid, db_path), today="2026-10-16", path=db_path) == main.PULL_AUTO_CAP
    assert db.get_series_by_id(sid, db_path)["on_pull_list"] == 1
