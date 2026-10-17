"""A credit without a Metron id gets the one its name has elsewhere on the shelf
(related.with_creator_ids). 2026-10-17: w0rldtr33 #23 carried LOCG's credits, so its
names weren't clickable though #20 credits the same four people with Metron ids."""
import json

import kometa.db as db
import kometa.related as rel


def _series(db_path, title, creators):
    sid = db.add_series(title=title, publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, ?)",
                  (sid, json.dumps([{"role": r, "name": n, "id": i} for r, n, i in creators]), "[]"))
    return sid


def test_a_credit_borrows_the_id_its_name_has_on_the_shelf(db_path):
    rel._ids_by_name.update(at=0.0, path=None, map={})
    _series(db_path, "w0rldtr33", [("writer", "James Tynion IV", 99), ("artist", "Fernando Blanco", 144)])
    _series(db_path, "Two of Them", [("writer", "John Smith", 1)])
    _series(db_path, "Other", [("writer", "John Smith", 2)])                    # one name, two people
    locg = [{"role": "Writer", "name": "James Tynion IV", "metron_creator_id": None, "people_id": "1860"},
            {"role": "Artist", "name": "fernando  blanco", "metron_creator_id": None},
            {"role": "Writer", "name": "John Smith", "metron_creator_id": None},
            {"role": "Letterer", "name": "Nobody Known", "metron_creator_id": None},
            {"role": "Colorist", "name": "Jordie Bellaire", "metron_creator_id": 13}]
    got = rel.with_creator_ids(locg, db_path)
    assert [c["metron_creator_id"] for c in got] == [99, 144, None, None, 13]
    assert got[0]["people_id"] == "1860"                                         # nothing else touched
