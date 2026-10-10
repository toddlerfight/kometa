"""People search (kometa/people.py): series by who made them."""
import json

import kometa.db as db
import kometa.people as people


def _signals(path, sid, creators):
    with db._connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS series_signals (tracked_series_id INTEGER PRIMARY KEY, creators_json TEXT, arcs_json TEXT, fetched_at TEXT)")
        c.execute("INSERT OR REPLACE INTO series_signals VALUES (?, ?, '[]', '2026-10-10')", (sid, json.dumps(creators)))


def _issue_credits(path, sid, credits, source="comicinfo"):
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as c:
        c.execute("INSERT INTO issue_record (tracked_series_id, number, credits_json, source) VALUES (?, 1, ?, ?)",
                  (sid, json.dumps(credits), source))


def test_people_from_both_sources_are_one_person_and_find_their_series(db_path):
    people._cache["value"] = None
    y100 = db.add_series(title="Batman - Year 100", publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
    hl = db.add_series(title="Heavy Liquid", publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
    at = db.add_series(title="Adventure Time", publisher="BOOM", folder_path=None, on_pull_list=False, path=db_path)
    _signals(db_path, y100, [{"role": "writer", "name": "Paul Pope", "id": 1116}, {"role": "colorist", "name": "José Villarrubia", "id": 1115},
                             {"role": "editor", "name": "Bob Schreck", "id": 9}])
    _issue_credits(db_path, hl, [{"role": "Artist", "name": "Paul Pope", "metron_creator_id": None}])
    _issue_credits(db_path, at, [{"role": "Cover", "name": "Paul Pope"}, {"role": "Colorist", "name": "Jose Villarrubia"}])
    out = people.search("pope", db_path)
    assert [(p["name"], p["id"], p["count"]) for p in out["people"]] == [("Paul Pope", 1116, 3)]
    assert out["series"] == {y100: "Writer: Paul Pope", hl: "Artist: Paul Pope", at: "Cover: Paul Pope"}
    # accents fold: one José, spelt with the accent
    assert [p["name"] for p in people.search("jose vill", db_path)["people"]] == ["José Villarrubia"]
    # each typed word needs its own part of the name
    assert people.search("paul p", db_path)["people"][0]["name"] == "Paul Pope"
    assert people.search("pope pope", db_path)["people"] == []
    # editors are not who made it
    assert people.search("schreck", db_path)["people"] == []
    assert people.search("po", db_path)["people"] == []          # too short to mean anyone
