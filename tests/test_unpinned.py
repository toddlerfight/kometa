"""Asking again for the runs no catalogue pinned down: proposals only, linked on approval."""
import kometa.db as db
from kometa import cvname, unpinned


def test_proposals_link_only_what_was_approved(db_path, monkeypatch):
    cvname.ensure_tables(db_path)
    a = db.add_series(title="Dark Entries HC", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    b = db.add_series(title="Gen 13 - The Maxx", publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        for sid in (a, b):
            conn.execute("INSERT INTO cv_volume_match (tracked_series_id, cv_volume_id, why, at) VALUES (?, NULL, 'x', '2026-10-17 00:00:00')", (sid,))
    db.set_gcd_link(b, "none", None, [], db_path)
    monkeypatch.setattr(unpinned, "_state", {"state": "done", "done": 2, "total": 2, "error": None, "proposals": [
        {"series_id": a, "title": "Dark Entries HC", "cv": {"id": 8, "why": "Dark Entries (2009, DC Comics)"}, "gcd": None},
        {"series_id": b, "title": "Gen 13 - The Maxx", "cv": {"id": 7, "why": "Gen13/The Maxx"}, "gcd": {"id": 5, "why": "Gen13/The Maxx"}},
    ]})
    assert unpinned.apply([b], db_path) == {"cv": 1, "gcd": 1}
    vols, _ = cvname.known(db_path)
    assert vols[b]["cv_volume_id"] == 7 and vols[a]["cv_volume_id"] is None      # 'Dark Entries HC' not approved
    s = db.get_series_by_id(b, db_path)
    assert s["gcd_link"] == "linked" and s["gcd_series_id"] == 5
