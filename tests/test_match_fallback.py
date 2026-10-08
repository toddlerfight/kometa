"""The matcher must never stall on what it can't answer (shelf_import._match):
junk titles and 'Metron unsure + LOCG shut' go to Needs matching, not round
and round the same three failures."""
import pytest

import kometa.db as db
import kometa.metron_client as mc
import kometa.shelf_import as si
import kometa.locg_client as lc


@pytest.fixture
def lib(db_path, monkeypatch):
    monkeypatch.setattr(si, "DB_PATH", db_path)
    monkeypatch.setattr(mc, "configured", lambda: True)
    monkeypatch.setattr(si, "THROTTLE_S", 0)
    return db_path


def test_numeric_folder_names_are_not_searched_anywhere(lib, monkeypatch):
    asked = []
    monkeypatch.setattr(si, "find_metron_match", lambda s: asked.append(("metron", s["title"])))
    monkeypatch.setattr(si, "find_confident_match", lambda s: asked.append(("locg", s["title"])))
    assert si._match({"title": "01", "publisher": "Marvel"}) == (None, None)
    assert si._match({"title": "#8 (2019)", "publisher": None}) == (None, None)
    assert asked == []


def test_metron_unsure_and_locg_paused_means_you_pick(lib, monkeypatch):
    monkeypatch.setattr(si, "find_metron_match", lambda s: None)
    monkeypatch.setattr(lc, "_pause", {"until": 4102444800.0})
    def boom(s):
        raise lc.LocgPaused("shut")
    monkeypatch.setattr(si, "find_confident_match", boom)
    assert si._match({"title": "Saga", "publisher": "Image"}) == (None, None)


def test_the_tick_keeps_moving_through_the_whole_list(lib, monkeypatch):
    monkeypatch.setattr(lc, "_pause", {"until": 4102444800.0})
    monkeypatch.setattr(si, "find_metron_match", lambda s: 7 if s["title"] == "Saga" else None)
    monkeypatch.setattr(si, "find_confident_match", lambda s: (_ for _ in ()).throw(lc.LocgPaused("shut")))
    monkeypatch.setattr(si, "sync_one_guarded", lambda *a, **k: True, raising=False)
    import kometa.sync as sync
    monkeypatch.setattr(sync, "sync_one_guarded", lambda *a, **k: True)
    for t in ("01", "06", "08", "Saga", "Nobody Knows"):
        sid = db.add_series(title=t, publisher="X", on_pull_list=False, path=lib)
        db.set_match_status(sid, si.PENDING, lib)
    done = si.match_pending(sleep=lambda s: None)
    statuses = {s["title"]: s["match_status"] for s in db.get_all_series(lib)}
    assert done["failed"] == 0
    assert statuses["Saga"] == si.AUTO
    assert {statuses[t] for t in ("01", "06", "08", "Nobody Knows")} == {si.NEEDS_MATCH}


def test_match_now_says_which_run(lib, monkeypatch):
    import kometa.sync as sync
    monkeypatch.setattr(sync, "sync_one_guarded", lambda *a, **k: True)
    rows = [{"id": 7, "title": "Batman: Damned", "publisher": "DC", "year": 2018, "issue_count": 3}]
    monkeypatch.setattr(mc, "search_series", lambda q: rows)
    sid = db.add_series(title="Batman - Damned", publisher="DC Comics", on_pull_list=False, path=lib)
    db.set_match_status(sid, si.PENDING, lib)
    r = si.match_one(sid)
    assert r["match_status"] == si.AUTO
    assert r["matched"] == {"source": "metron", "id": 7, "title": "Batman: Damned", "publisher": "DC",
                            "year": 2018, "issue_count": 3}
    assert db.get_series_by_id(sid, lib)["metron_series_id"] == 7
