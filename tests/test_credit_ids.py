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


def test_a_name_the_shelf_doesnt_know_is_asked_of_metron_once_and_kept(db_path, monkeypatch):
    rel._ids_by_name.update(at=0.0, path=None, map={})
    asked = []

    def search(q):
        asked.append(q)
        return {"Hassan Otsmane-Elhaou": [{"id": 27980, "name": "Hassan Otsmane-Elhaou"}],
                "Javier Rodriguez": [{"id": 1136, "name": "Javier Rodríguez"}],          # accents aside, the same person
                "John Smith": [{"id": 1, "name": "John Smith"}, {"id": 2, "name": "John Smith"}],
                "Nobody": []}.get(q, [])
    assert rel.resolve_creator_names(["Hassan Otsmane-Elhaou", "Javier Rodriguez", "John Smith", "Nobody"], db_path, search) == 2
    got = rel.with_creator_ids([{"name": n, "metron_creator_id": None} for n in ("Hassan Otsmane-Elhaou", "Javier Rodriguez", "John Smith")],
                               db_path, ask=False)
    assert [c["metron_creator_id"] for c in got] == [27980, 1136, None]     # two people by one name: left alone
    # asked once: a later look at the same names doesn't ask again (a miss is kept for a month)
    started = []
    monkeypatch.setattr(rel, "_resolve_in_background", lambda names, path: started.append(names))
    import kometa.metron_client as mc
    monkeypatch.setattr(mc, "configured", lambda: True)
    rel.with_creator_ids([{"name": "John Smith"}, {"name": "Nobody"}, {"name": "Someone New"}], db_path)
    assert started == [["Someone New"]]


def test_a_reader_account_never_starts_a_lookup(db_path, monkeypatch):
    rel._ids_by_name.update(at=0.0, path=None, map={})
    started = []
    monkeypatch.setattr(rel, "_resolve_in_background", lambda names, path: started.append(names))
    rel.with_creator_ids([{"name": "Someone New"}], db_path, ask=False)
    assert started == []


def test_the_trade_window_gets_the_same(db_path, monkeypatch):
    import kometa.main as main
    import kometa.record as record
    rel._ids_by_name.update(at=0.0, path=None, map={})
    _series(db_path, "w0rldtr33", [("writer", "James Tynion IV", 99)])
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(record, "trade_details", lambda lid: {"desc": "", "credits": [{"role": "Writer", "name": "James Tynion IV"}]})
    import kometa.trade_community as tc
    monkeypatch.setattr(tc, "for_trade", lambda lid: None)
    assert main.get_trade_details("123")["credits"][0]["metron_creator_id"] == 99
