"""No cover, no suggestion (the owner, 2026-10-17: "I'm clearly asking for no blank
covers"): a catalogue run with no cover isn't offered in a More-from row."""
import json

import kometa.db as db
import kometa.related as rel
from tests.test_related import _series_ids


def test_a_catalogue_run_without_a_cover_is_not_suggested(db_path):
    pope = ("artist", "Paul Pope", 7)
    me = _series_ids(db_path, "Batman - Year 100", [("writer", "Paul Pope", 7), pope])
    for t in ("100%", "Heavy Liquid"):
        _series_ids(db_path, t, [pope])
    assert rel._cached_works(7, db_path) is None                                  # creates the table
    with db._connect(db_path) as c:
        c.execute("INSERT INTO creator_works (creator_id, works_json) VALUES (7, ?)", (json.dumps([
            {"metron_series_id": 501, "title": "THB", "year": 1994, "count": 6, "cover": "https://m/thb.jpg"},
            {"metron_series_id": 502, "title": "Escapo", "year": 1996, "count": 2, "cover": "https://m/escapo.jpg"},
            {"metron_series_id": 505, "title": "Coverless", "year": 2001, "count": 4, "cover": None}]),))
    rows, _ = rel.creator_rows(me, path=db_path)
    titles = [x["title"] for x in rows[0]["items"]]
    assert "Coverless" not in titles and {"THB", "Escapo"} <= set(titles)
