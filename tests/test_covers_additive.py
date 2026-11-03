"""Covers from different sources ADD to one another; no source's silence or empty answer removes or
overwrites what another gave (the owner, 2026-11-04). LOCG's image is on S3, so a walled page never
means 'no LOCG cover' — the S3 path is tried whenever nothing else holds the issue's main cover."""
import kometa.db as db
import kometa.record as rec

S3 = "https://s3.amazonaws.com/comicgeeks/comics/covers/{}-{}.jpg"


def _issue(db_path, lid="3951157", mid=181715):
    sid = db.add_series(title="Midnight Spider-Man", publisher="Marvel", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2026-10-07", 0, locg_issue_id=lid, metron_issue_id=mid, path=db_path)
    return sid, db.get_issues_for_series(sid, db_path)[0]


def _fill(sid, issue, db_path, metron_covers, *, locg_open, locg=None):
    return rec.fill_variants(sid, 1.0, db_path, force=True, issue=issue,
                             metron=lambda i, p: {"covers": metron_covers},
                             locg=locg or (lambda lid: (_ for _ in ()).throw(AssertionError("LOCG's page was asked"))),
                             locg_open=lambda: locg_open)


def test_walled_locg_and_no_other_cover_still_gets_the_main_from_s3(db_path):
    sid, issue = _issue(db_path)
    covers = _fill(sid, issue, db_path, [], locg_open=False)
    assert [(c["id"], c["source"], c["large"]) for c in covers] == [("3951157", "locg", S3.format("large", "3951157"))]
    assert rec.get_issue(sid, 1.0, db_path)["covers"][0]["thumb"] == S3.format("medium", "3951157")


def test_a_page_that_errors_is_treated_the_same_as_a_walled_one(db_path):
    sid, issue = _issue(db_path)
    boom = lambda lid: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
    covers = _fill(sid, issue, db_path, [], locg_open=True, locg=boom)
    assert [c["id"] for c in covers] == ["3951157"]


def test_s3_never_replaces_what_metron_already_gave(db_path):
    sid, issue = _issue(db_path)
    metron = [{"id": "m181715", "name": "Cover A (Main)", "thumb": "mt", "large": "ml"}]
    covers = _fill(sid, issue, db_path, metron, locg_open=False)
    assert [c["id"] for c in covers] == ["m181715"]            # Metron's main stands; no second main beside it


def test_covers_from_two_sources_are_both_kept_when_they_are_different_pictures(db_path):
    sid, issue = _issue(db_path)
    metron = [{"id": "m1", "name": "Cover A (Main)", "thumb": "mt", "large": "ml"},
              {"id": "m2", "name": "Cover B", "thumb": "mt2", "large": "ml2"}]
    locg = lambda lid: [{"id": "3951157", "name": "Cover A (Main)", "thumb": "lt", "large": "ll"},
                        {"id": "v9", "name": "NYCC 2026 Variant", "thumb": "lt9", "large": "ll9"}]
    covers = _fill(sid, issue, db_path, metron, locg_open=True, locg=locg)
    assert [c["id"] for c in covers] == ["m1", "m2", "v9"]


def test_the_same_picture_under_a_different_name_is_not_listed_twice(db_path):
    sid, issue = _issue(db_path)
    shared = S3.format("large", "v9")
    metron = [{"id": "m1", "name": "Cover A (Main)", "thumb": "mt", "large": "ml"},
              {"id": "m5", "name": "Store Exclusive", "thumb": "x", "large": shared}]
    locg = lambda lid: [{"id": "v9", "name": "Some Retailer Variant", "thumb": "y", "large": shared}]
    covers = _fill(sid, issue, db_path, metron, locg_open=True, locg=locg)
    assert [c["id"] for c in covers] == ["m1", "m5"]           # one picture, listed once (by image URL)


def test_locg_covers_from_an_earlier_answer_survive_a_walled_refresh(db_path):
    sid, issue = _issue(db_path)
    metron = [{"id": "m1", "name": "Cover A (Main)", "thumb": "mt", "large": "ml"}]
    locg = lambda lid: [{"id": "v9", "name": "NYCC 2026 Variant", "thumb": "lt9", "large": "ll9"}]
    _fill(sid, issue, db_path, metron, locg_open=True, locg=locg)
    again = _fill(sid, issue, db_path, metron, locg_open=False)
    assert [c["id"] for c in again] == ["m1", "v9"]


def test_a_sync_that_brings_no_image_cannot_wipe_the_one_stored(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_rows([(sid, 1.0, "2012-03-14", False, "https://static.metron.cloud/a.jpg", "11")], path=db_path)
    db.upsert_issue_rows([(sid, 1.0, "2012-03-14", False, None, "11")], path=db_path)          # LOCG-only pass, no art
    assert db.get_issues_for_series(sid, db_path)[0]["metron_image"] == "https://static.metron.cloud/a.jpg"
    db.upsert_issue_rows([(sid, 1.0, "2012-03-14", False, "https://static.metron.cloud/b.jpg", "11")], path=db_path)
    assert db.get_issues_for_series(sid, db_path)[0]["metron_image"] == "https://static.metron.cloud/b.jpg"   # a real new one still lands
