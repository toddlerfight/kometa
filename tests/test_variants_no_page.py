"""An issue Metron doesn't have gets its Variants answer at once — what's stored, else LOCG's
main cover from S3 — never LOCG's walled issue page (SQUEE!: 10 s, then a 502)."""
import kometa.db as db
import kometa.record as record
from kometa import issue_meta, locg_client


def test_a_locg_only_issue_answers_without_asking_the_page(db_path, monkeypatch):
    sid = db.add_series(title="SQUEE!", publisher="SLG Publishing", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "1997-04-01", 1, locg_issue_id="7898555", path=db_path)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LOCG's page was asked"))
    monkeypatch.setattr(locg_client, "fetch_variants", boom)
    monkeypatch.setattr(issue_meta, "details_for", boom)
    monkeypatch.setattr(record, "fill_variants", boom)
    issue = db.get_issues_for_series(sid, db_path)[0]
    out = record.variants(issue, db_path)
    assert out["covers"] == [{"id": "7898555", "name": "Cover A (Main)",
                              "thumb": "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-7898555.jpg",
                              "large": "https://s3.amazonaws.com/comicgeeks/comics/covers/large-7898555.jpg"}]
