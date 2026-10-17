"""A recent issue whose record has no credits is asked again (record._fresh / pending_issues).
2026-10-17: 100 Bullets: The US of Anger #4 was filled from Metron on its release day,
before Metron had credits for it, and the row counted as fresh for 90 days."""
from datetime import date, timedelta

import kometa.db as db
import kometa.record as record


def _issue(db_path, sid, n, out_days_ago, credits, fetched_days_ago):
    store = (date.today() - timedelta(days=out_days_ago)).isoformat()
    db.upsert_issue_status(sid, n, store, 1, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE issue_status SET metron_issue_id = ? WHERE tracked_series_id = ? AND number = ?", (int(1000 + n), sid, n))
    record.write_issue(sid, n, {"desc": "x", "credits": credits, "store_date": store}, "metron", "full",
                       {"metron_issue_id": int(1000 + n), "store_date": store}, db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE issue_record SET fetched_at = datetime('now', ?), extra_json = '{}' WHERE tracked_series_id = ? AND number = ?",
                  (f"-{fetched_days_ago} days", sid, n))
    return record.get_issue(sid, n, db_path)


def test_a_recent_issue_with_no_credits_is_asked_again_daily(db_path):
    sid = db.add_series(title="100 Bullets: The US of Anger", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    who = [{"role": "Writer", "name": "Brian Azzarello"}]
    thin = _issue(db_path, sid, 4.0, out_days_ago=3, credits=[], fetched_days_ago=2)
    today = _issue(db_path, sid, 5.0, out_days_ago=0, credits=[], fetched_days_ago=0)
    full = _issue(db_path, sid, 3.0, out_days_ago=10, credits=who, fetched_days_ago=2)
    old = _issue(db_path, sid, 1.0, out_days_ago=4000, credits=[], fetched_days_ago=2)
    assert not record._fresh(thin)                         # out lately, no credits, asked over a day ago
    assert record._fresh(today) and record._fresh(full)    # asked today / has its credits
    assert record._fresh(old)                              # a back issue Metron never credited: not asked daily
    pending = {r["number"] for r in record.pending_issues(db_path) if r["tracked_series_id"] == sid}
    assert pending == {4.0}
