"""A person's work by what they did on it (related.role_groups / creator_sections / _top_creators).
2026-10-17: Javier Rodríguez's creator page mixed his art with books he only coloured, and on
Batgirl: Year One two co-writers took both 'More from' rows — the artist got none, and the
colorist (Javier) couldn't get one at all."""
import json

import kometa.db as db
import kometa.related as rel


def _series(db_path, title, creators):
    sid = db.add_series(title=title, publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, ?)",
                  (sid, json.dumps([{"role": r, "name": n, "id": i} for r, n, i in creators]), "[]"))
    return sid


def _works(db_path, cid, works):
    rel._cached_works(cid, db_path)                       # creates the table
    with db._connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO creator_works (creator_id, works_json) VALUES (?, ?)", (cid, json.dumps(works)))


def test_roles_fall_into_groups_highest_first():
    assert rel.role_groups(["colorist", "Artist", "cover"]) == ["artist", "colorist", "cover"]
    assert rel.role_groups(["Artist, Colorist"]) == ["artist", "colorist"]         # LOCG writes them as one
    assert rel.role_groups(["Penciller"]) == ["artist"] and rel.role_groups(["Story"]) == ["writer"]
    assert rel.role_groups([]) == ["other"] and rel.role_groups(["Editor"]) == ["other"]


def test_the_creator_page_is_sectioned_by_role_each_work_once(db_path):
    me = ("artist", "Javier Rodríguez", 558)
    _series(db_path, "Absolute Martian Manhunter", [me, ("colorist", "Javier Rodríguez", 558), ("cover", "Javier Rodríguez", 558)])
    _series(db_path, "Batgirl Year One", [("colorist", "Javier Rodríguez", 558), ("writer", "Chuck Dixon", 1)])
    _works(db_path, 558, [
        {"metron_series_id": 1, "title": "Daredevil", "year": 2011, "count": 9, "cover": "https://m/dd.jpg", "roles": ["colorist"]},
        {"metron_series_id": 2, "title": "Exiles", "year": 2018, "count": 6, "cover": "https://m/ex.jpg", "roles": ["artist"]},
        {"metron_series_id": 3, "title": "Legion", "year": 2018, "count": 3, "cover": "https://m/lg.jpg", "roles": ["cover"], "cover_only": True},
        {"metron_series_id": 4, "title": "Mystery", "year": 2020, "count": 2, "cover": "https://m/my.jpg", "roles": []}])
    page = rel.creator_page(558, path=db_path)
    got = [(s["label"], [(x["title"], x["why"]) for x in s["items"]]) for s in page["sections"]]
    assert got == [("Artist", [("Absolute Martian Manhunter", ["Also colorist, cover"]), ("Exiles", [])]),
                   ("Colorist", [("Batgirl Year One", []), ("Daredevil", [])]),
                   ("Covers", [("Legion", [])]),
                   ("Other credits", [("Mystery", [])])]
    assert [x["title"] for x in page["shelf"]] == ["Absolute Martian Manhunter", "Batgirl Year One"]   # the old lists stay for older apps


def test_a_co_written_book_still_shows_its_artist_and_a_known_colorist(db_path):
    team = [("writer", "Chuck Dixon", 1), ("writer", "Scott Beatty", 2), ("penciller", "Marcos Martín", 3),
            ("inker", "Álvaro López", 4), ("colorist", "Javier Rodríguez", 558), ("letterer", "Willie Schubert", 6)]
    me = _series(db_path, "Batgirl Year One", team)
    for cid, n in ((1, "Chuck Dixon"), (2, "Scott Beatty"), (3, "Marcos Martín"), (558, "Javier Rodríguez")):
        _works(db_path, cid, [{"metron_series_id": cid * 100 + k, "title": f"{n} work {k}", "year": 2000 + k, "count": 3,
                               "cover": f"https://m/{cid}-{k}.jpg", "roles": ["artist" if k % 2 else "colorist"]} for k in range(5)])
    rows, _ = rel.creator_rows(me, path=db_path)
    assert [r["name"] for r in rows] == ["Chuck Dixon", "Marcos Martín", "Scott Beatty", "Javier Rodríguez"]
    jav = rows[-1]["items"]
    assert [rel.role_groups(x["roles"])[0] for x in jav][:3] == ["colorist"] * 3      # coloured here: his colour work leads


def test_a_colorists_catalogue_isnt_fetched_for_a_row(db_path, monkeypatch):
    me = _series(db_path, "Batgirl Year One", [("writer", "Chuck Dixon", 1), ("colorist", "Unknown Colorist", 77)])
    asked = []
    monkeypatch.setattr(rel, "_fill_works_in_background", lambda ids, path: asked.append(ids))
    _works(db_path, 1, [])
    rows, pending = rel.creator_rows(me, path=db_path)
    assert asked == [] and not pending
