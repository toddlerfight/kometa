"""LOCG's cover images live on S3, outside LOCG's wall (docs/locg-covers-s3-solution-2026-10-18.md):
an issue with no catalogue art takes large-<locg id>.jpg straight from there, and the web page is
never asked. Buffy the Vampire Slayer #2–#10 (1998) sat as blank frames while it answered 200."""
from fastapi import Response

import kometa.db as db
import kometa.thumbnails as th
from kometa import images, locg_client, sync


def test_an_artless_issue_takes_its_cover_from_s3_and_never_asks_the_page(db_path, monkeypatch):
    monkeypatch.setattr(th, "DB_PATH", db_path)
    th._thumb_misses.clear()
    sid = db.add_series(title="Buffy the Vampire Slayer", publisher="Dark Horse Comics", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 10.0, "1999-06-23", 0, locg_issue_id="8504205", path=db_path)
    asked = []
    monkeypatch.setattr(images, "serve", lambda key, url, source, path=None, **k: asked.append((url, source)) or Response(b"jpg", media_type="image/jpeg"))
    monkeypatch.setattr(locg_client, "fetch_variants", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the page was asked")))
    resp = th._issue_thumbnail(sid, 10.0)
    assert resp.status_code == 200
    assert asked == [("https://s3.amazonaws.com/comicgeeks/comics/covers/large-8504205.jpg", "locg")]


def test_a_run_off_the_pull_list_still_gets_locgs_list_when_metron_left_issues_artless():
    run = {"on_pull_list": 0, "locg_fetched_at": None}
    assert not sync._locg_issues_due(run, have_metron=True)                      # nothing missing: Metron's enough
    assert sync._locg_issues_due(run, have_metron=True, artless=True)            # artless issues: LOCG's covers + ids
    recent = {"on_pull_list": 0, "locg_fetched_at": "2999-01-01 00:00:00"}
    assert not sync._locg_issues_due(recent, have_metron=True, artless=True)     # weekly, not every sync
