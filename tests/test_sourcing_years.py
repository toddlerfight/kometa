"""A run that began decades before yours leaves the 'Is this the same comic?' card
(sourcing.YEARS_BEFORE_MAX), and an unlinked run you're pulling is asked about weekly
(rematch.RECHECK_DAYS_PULLING). 2026-10-17: Batman: Shadow of the Bat (2027, not out yet)
was offered the 1992 run, and would have waited a month to link the new one."""
import json

import kometa.db as db
import kometa.rematch as rm
import kometa.sourcing as src

SOTB = {"title": "Batman: Shadow of the Bat", "publisher": "DC Comics", "year_began": 2027}


def test_a_run_decades_before_yours_is_not_a_candidate():
    assert src.score_candidate(SOTB, {"title": "Batman: Shadow of the Bat", "publisher": "DC Comics", "year": 1992, "issue_count": 96}) == 0.0
    assert src.score_candidate(SOTB, {"title": "Batman: Shadow of the Bat", "publisher": "DC Comics", "year": 2027, "issue_count": 1}) >= 80
    # later is still possible (a collected edition), and a few years earlier still scores
    hush = {"title": "Batman: Hush", "publisher": "DC Comics", "year_began": 2003}
    assert src.score_candidate(hush, {"title": "Batman: Hush 20th Anniversary Edition HC", "publisher": "DC Comics", "year": 2022, "issue_count": 1}, shelf_total=0) > 0


def test_the_deck_is_rescored_once_and_the_file_check_stands(db_path, monkeypatch):
    monkeypatch.setattr(src, "DB_PATH", db_path)
    sid = db.add_series(title="Batman: Shadow of the Bat", publisher="DC Comics", year_began=2027, folder_path=None, on_pull_list=True, path=db_path)
    other = db.add_series(title="East of West", publisher="Image Comics", year_began=2013, folder_path=None, on_pull_list=False, path=db_path)
    old = [{"id": 2172, "title": "Batman: Shadow of the Bat", "publisher": "DC Comics", "year": 1992, "issue_count": 96,
            "score": 70.0, "confidence": "moderate", "is_edition": False, "files": "none"}]
    keep = [{"id": 9, "title": "East of West", "publisher": "Image", "year": 2013, "issue_count": 45,
             "score": 100.0, "confidence": "high", "is_edition": False, "files": "year"}]
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET locg_series_id = 198527, metron_candidates_json = ? WHERE id = ?", (json.dumps(old), sid))
        c.execute("UPDATE tracked_series SET locg_series_id = 1, metron_candidates_json = ? WHERE id = ?", (json.dumps(keep), other))
    src.ensure_deck_rescored(db_path)
    get = lambda i: json.loads(db.get_series_by_id(i, db_path)["metron_candidates_json"])
    assert get(sid) == []                                          # the card goes: nothing plausible left
    assert get(other)[0]["files"] == "year"                        # a checked card keeps its check


def test_a_run_youre_pulling_is_asked_about_weekly(db_path):
    def unlinked(title, pull, days_ago):
        sid = db.add_series(title=title, publisher="DC Comics", folder_path=None, on_pull_list=pull, path=db_path)
        db.set_metron_link(sid, "none", db_path)
        with db._connect(db_path) as c:
            c.execute("UPDATE tracked_series SET metron_checked_at = datetime('now', ?) WHERE id = ?", (f"-{days_ago} days", sid))
        return sid
    unlinked("Pulled, asked 8 days ago", True, 8)
    unlinked("Pulled, asked 3 days ago", True, 3)
    unlinked("Not pulled, asked 8 days ago", False, 8)
    unlinked("Not pulled, asked 31 days ago", False, 31)
    assert [s["title"] for s in rm.candidates(db_path)] == ["Pulled, asked 8 days ago", "Not pulled, asked 31 days ago"]
