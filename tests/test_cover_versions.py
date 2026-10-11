"""Covers after the Komga retirement (2026-10-10): the Library card asks
Kometa's own issue route, and the last-resort Komga thumbnail keeps its old
cache key."""
import kometa.db as db
from kometa.thumbnails import _book_cache_key


def test_card_image_is_kometas_own_issue_route(db_path, series):
    db.upsert_issue_status(series, 1.0, "2012-03-14", owned=True, komga_book_id="BOOK1", path=db_path)
    card = db.get_all_series_summaries(db_path)[series]["card_image"]
    assert card == f"/api/series/{series}/issues/1/thumbnail"


def test_komga_fallback_cache_key():
    assert _book_cache_key("B", None) == "komga:book:B"
