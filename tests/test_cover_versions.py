"""A Komga book id outlives the file behind it. Curse Words #015 was swapped
for a complete copy at the same path, Komga kept the id, and the bare
'/api/book/<id>/thumbnail' URL kept serving the coverless one — off our disk
cache and out of every browser told to hold it for 30 days."""
import kometa.db as db
from kometa.sync import komga_book_version
from kometa.thumbnails import _book_cache_key


def test_version_tracks_file_mtime_and_size():
    a = komga_book_version({"fileLastModified": "2026-09-19T10:15:56Z", "sizeBytes": 31118018})
    b = komga_book_version({"fileLastModified": "2026-09-19T11:22:03Z", "sizeBytes": 63436041})
    assert a and b and a != b
    assert komga_book_version({}) is None


def test_issue_rows_carry_the_book_version(db_path, series):
    db.upsert_issue_status(series, 15.0, "2018-09-19", owned=True, komga_book_id="BOOK15", path=db_path)
    row = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 15.0)
    assert row["komga_book_v"] is None                  # unknown until a sync sees it

    db.set_komga_book_versions({"BOOK15": "t1:100"}, db_path)
    db.set_komga_book_versions({"BOOK15": "t2:200"}, db_path)     # file replaced
    row = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 15.0)
    assert row["komga_book_v"] == "t2:200"


def test_card_image_is_versioned(db_path, series):
    db.upsert_issue_status(series, 1.0, "2012-03-14", owned=True, komga_book_id="BOOK1", path=db_path)
    db.set_komga_book_versions({"BOOK1": "2026-09-19T11:22:03Z:63436041"}, db_path)
    card = db.get_all_series_summaries(db_path)[series]["card_image"]
    assert card == "/api/book/BOOK1/thumbnail?v=2026-09-19T11%3A22%3A03Z%3A63436041"


def test_cache_key_changes_with_the_file():
    assert _book_cache_key("B", None) == "komga:book:B"   # legacy entries stay warm
    assert _book_cache_key("B", "t1:1") != _book_cache_key("B", "t2:2")
