"""Credits that arrive by name only (LOCG, GCD, Comic Vine) carry no Metron person id; the
issue card looked the id up as it drew, the stored signals didn't — so Rafael Grampá's page
never listed The Dark Knight Returns- the Golden Child, whose credits came from LOCG, though
the card linked his name to that very page (2026-10-17)."""
import json

import kometa.db as db
import kometa.record as record
import kometa.related as rel

GRAMPA = 1486


def _shelf_titles(db_path, cid):
    return {x["title"] for x in rel.creator_page(cid, path=db_path)["shelf"]}


def _run(db_path, title, credits, source, n=1.0):
    sid = db.add_series(title=title, publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, n, "2019-12-11", 1, path=db_path)
    record.write_issue(sid, n, {"desc": "", "credits": credits, "extra": {}}, source, "full", {}, db_path)
    return sid


def test_a_name_only_run_gets_the_id_the_shelf_knows_and_shows_on_the_page(db_path, monkeypatch):
    monkeypatch.setattr(rel, "_fill_works_in_background", lambda ids, path: None)
    rel._ids_by_name.update(at=0)
    gargoyle = _run(db_path, "Batman: Gargoyle of Gotham", [
        {"role": "Writer", "name": "Rafael Grampá", "metron_creator_id": GRAMPA},
        {"role": "Artist", "name": "Rafael Grampá", "metron_creator_id": GRAMPA}], "metron")
    rel.fill_signals(gargoyle, db_path)
    rel._ids_by_name.update(at=0)
    golden = _run(db_path, "The Dark Knight Returns- the Golden Child Deluxe Edition", [
        {"role": "Writer", "name": "Frank Miller", "metron_creator_id": None},
        {"role": "Artist", "name": "Rafael Grampá", "metron_creator_id": None}], "locg")
    rel.fill_signals(golden, db_path)
    assert _shelf_titles(db_path, GRAMPA) == {"Batman: Gargoyle of Gotham", "The Dark Knight Returns- the Golden Child Deluxe Edition"}
    # a name nobody has given an id stays without one: never a guess
    sig = rel._signals(db_path)[golden]["creators"]
    assert next(c for c in sig if c["name"] == "Frank Miller")["id"] is None


def test_signals_stored_before_are_filled_in_by_the_refresh(db_path, monkeypatch):
    monkeypatch.setattr(rel, "_fill_works_in_background", lambda ids, path: None)
    rel._ids_by_name.update(at=0)
    rel.ensure_tables(db_path)
    a = db.add_series(title="Batman: Gargoyle of Gotham", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    b = db.add_series(title="Golden Child", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, '[]')",
                  (a, json.dumps([{"role": "artist", "name": "Rafael Grampá", "id": GRAMPA}])))
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, '[]')",
                  (b, json.dumps([{"role": "artist", "name": "Rafael Grampa", "id": None}])))   # accents aside, the same name
    assert _shelf_titles(db_path, GRAMPA) == {"Batman: Gargoyle of Gotham"}
    assert rel.backfill_signal_ids(db_path) == 1
    assert _shelf_titles(db_path, GRAMPA) == {"Batman: Gargoyle of Gotham", "Golden Child"}
    assert rel.backfill_signal_ids(db_path) == 0                       # nothing left to fill: no rewrite


def test_a_name_two_people_share_is_left_alone(db_path, monkeypatch):
    rel._ids_by_name.update(at=0)
    rel.ensure_tables(db_path)
    ids = [db.add_series(title=t, publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path) for t in ("A", "B", "C")]
    with db._connect(db_path) as c:
        for sid, cid in zip(ids, (11, 12, None)):
            c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, '[]')",
                      (sid, json.dumps([{"role": "writer", "name": "John Smith", "id": cid}])))
    assert rel.backfill_signal_ids(db_path) == 0


def test_the_card_hears_when_its_names_were_just_sent_to_metron(db_path, monkeypatch):
    from kometa import metron_client
    sent = []
    monkeypatch.setattr(metron_client, "configured", lambda: True)
    monkeypatch.setattr(rel, "_resolve_in_background", lambda names, path: sent.extend(names))
    rel._ids_by_name.update(at=0)
    credits = [{"role": "Writer", "name": "Jhonen Vasquez", "metron_creator_id": None}]
    out, asking = rel.with_creator_ids_asking(credits, db_path, ask=True)
    assert asking and sent == ["Jhonen Vasquez"] and out[0]["metron_creator_id"] is None
    _, asking = rel.with_creator_ids_asking(credits, db_path, ask=False)          # a reader never asks
    assert not asking
