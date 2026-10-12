"""Covers after the Komga retirement: the Library card asks Kometa's own issue
route, never a Komga book thumbnail."""
import kometa.db as db


def test_card_image_is_kometas_own_issue_route(db_path, series):
    db.upsert_issue_status(series, 1.0, "2012-03-14", owned=True, path=db_path)
    card = db.get_all_series_summaries(db_path)[series]["card_image"]
    assert card == f"/api/series/{series}/issues/1/thumbnail"
