"""'No run' (Needs matching → none) is about Metron and LOCG; GCD gets its one cautious look
at those runs too (the owner, 2026-10-17: 113 had never been asked)."""
import kometa.db as db
from kometa import harvest


def test_a_run_you_said_has_no_run_is_still_a_gcd_candidate(db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    sid = db.add_series(title="They Called Us Enemy", publisher="Top Shelf Productions", on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET match_status = 'manual', metron_link = 'none', locg_link = 'none' WHERE id = ?", (sid,))
    assert sid in [s["id"] for s in harvest.gcd_link_candidates(db_path)]
    db.set_gcd_link(sid, "none", None, [], db_path)                     # asked once: remembered, not asked again
    assert sid not in [s["id"] for s in harvest.gcd_link_candidates(db_path)]
