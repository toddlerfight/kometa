"""Issues with no catalogue art get Comic Vine's cover on their tile (Buffy the Vampire
Slayer, 1998: Metron has no art for #1–#10, LOCG's pages are walled — blank frames)."""
import json

import kometa.db as db
import kometa.record as record
from kometa import harvest


def _issue(db_path, sid, n, owned, art=None):
    db.upsert_issue_status(sid, float(n), "1998-10-28", owned, metron_image=art, path=db_path)
    record.write_issue(sid, float(n), {"desc": "", "credits": [], "extra": {"cv_id": 1000 + n}}, "metron", "full",
                       {"metron_issue_id": n}, db_path)


def test_artless_issues_are_asked_first_and_arted_unowned_ones_not_at_all(db_path):
    sid = db.add_series(title="Buffy the Vampire Slayer", publisher="Dark Horse Comics", folder_path=None, on_pull_list=False, path=db_path)
    _issue(db_path, sid, 1, 1)                                     # owned: for the archive, as before
    _issue(db_path, sid, 2, 0)                                     # not owned, no art: blank on screen
    _issue(db_path, sid, 11, 0, art="https://static.metron.cloud/media/issue/11.jpg")   # has art: not asked
    got = [(t["number"], t["cv_id"]) for t in harvest.cv_cover_candidates(db_path)]
    assert got == [(2.0, "1002"), (1.0, "1001")]

    class FakeCV:
        def get_issue_originals(self, ids):
            return {i: f"https://comicvine.gamespot.com/a/uploads/original/{i}.jpg" for i in ids}
    db.set_config({"cv_api_key": "k"}, db_path)
    assert harvest.cv_covers_tick(path=db_path, client=FakeCV()) == 2
    assert json.loads(json.dumps(record.get_issue(sid, 2.0, db_path)["extra"]))["cv_original"].endswith("/1002.jpg")
    assert harvest.cv_cover_candidates(db_path) == []              # asked once each
