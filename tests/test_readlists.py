"""Reading lists (kometa/readlists.py): CBL in, books on the shelf out, in order."""
import os

import pytest

import kometa.db as db
import kometa.readlists as rl
from tests.conftest import make_cbz

CBL = b"""<?xml version="1.0" encoding="utf-8"?>
<ReadingList><Name>Hellboy TPB order</Name><NumIssues>5</NumIssues><Books>
<Book Series="Hellboy: Seed of Destruction" Number="1" Volume="1994" Year="1994"><Database Name="cv" Series="33634" Issue="218367" /></Book>
<Book Series="B.P.R.D.: Plague of Frogs" Number="1" Volume="2005" Year="2005" />
<Book Series="Sir Edward Grey, Witchfinder" Number="2" Volume="2012" Year="2012" />
<Book Series="Sir Edward Grey, Witchfinder" Number="3" Volume="2012" Year="2015" />
<Book Series="Hellboy: The Chained Coffin and Others" Number="1" Volume="1998" Year="1998" />
</Books></ReadingList>"""


def _shelf(db_path, root, title, files, tracked=None):
    folder = root / title
    folder.mkdir(parents=True)
    for f in files:
        make_cbz(folder / f)
    sid = db.upsert_shelf_series(str(folder), title, "Dark Horse", tracked, len(files), "2026-10-09T00:00:00Z", db_path)
    from kometa.naming import parse_issue_number
    db.index_books([(str(folder / f), 10, 1.0, parse_issue_number(f, title), sid, tracked) for f in files], db_path)
    return sid


@pytest.fixture
def shelf(db_path, tmp_path):
    root = tmp_path / "comics" / "Dark Horse Comics"
    _shelf(db_path, root, "Hellboy - Seed of Destruction",
           ["Hellboy - Seed of Destruction #001.cbz", "Hellboy - Seed of Destruction #002.cbz"])
    _shelf(db_path, root, "BPRD - Plague of Frogs", ["BPRD - Plague of Frogs #001.cbz", "BPRD - Plague of Frogs TPB (2005).cbz"])
    _shelf(db_path, root, "Sir Edward Grey, Witchfinder",
           ["Sir Edward Grey, Witchfinder #001.cbz", "Sir Edward Grey, Witchfinder #002.cbz",
            "Sir Edward Grey, Witchfinder #002.cbr"])
    return db_path


def test_import_resolves_whole_runs_single_volumes_and_gaps(shelf):
    lid = rl.import_cbl(CBL, "hellboy.cbl", path=shelf)
    r = rl.resolve(lid, path=shelf)
    assert r["name"] == "Hellboy TPB order" and r["total"] == 5
    e = {x["position"]: x for x in r["entries"]}
    # one entry for the series → the whole run, in order
    assert e[1]["status"] == "owned" and [b["label"] for b in e[1]["books"]] == ["#1", "#2"] and e[1]["expanded"]
    # 'B.P.R.D.' finds the 'BPRD' folder; the trade beside the single is the same pages, not a second book
    assert e[2]["status"] == "owned" and [b["label"] for b in e[2]["books"]] == ["#1"]
    # several entries for one series → that numbered book each; CBZ beats CBR
    assert e[3]["status"] == "owned" and e[3]["books"][0]["label"] == "#2"
    assert not e[3]["books"][0].get("expanded")
    assert e[4]["status"] == "missing" and e[4]["books"] == []        # #3 not on the shelf yet
    assert e[5]["status"] == "not_on_shelf"
    assert r["owned"] == 3 and r["read"] == 0
    assert r["continue"] == e[1]["books"][0]["id"]
    # the reader's next walks the flattened order across entries
    seq = rl.order(lid, path=shelf)
    assert len(seq) == 4
    assert rl.next_book(lid, seq[0], path=shelf) == seq[1]
    assert rl.next_book(lid, seq[-1], path=shelf) is None
    assert rl.next_book(lid, 999999, path=shelf) is None


def test_reimport_replaces_and_continue_skips_finished(shelf):
    lid = rl.import_cbl(CBL, path=shelf)
    assert rl.import_cbl(CBL, path=shelf) == lid
    assert len(rl.get_items(lid, path=shelf)) == 5
    seq = rl.order(lid, path=shelf)
    db.set_progress("me", seq[0], 10, True, "2026-10-09T00:00:00Z", shelf)
    r = rl.resolve(lid, path=shelf)
    assert r["continue"] == seq[1]
    rl.delete_list(lid, path=shelf)
    assert rl.get_lists(path=shelf) == []


def test_bad_cbl_is_refused(shelf):
    with pytest.raises(rl.CblError):
        rl.import_cbl(b"<html>nope</html>", path=shelf)
    with pytest.raises(rl.CblError):
        rl.import_cbl(b"<ReadingList><Name>x</Name><Books/></ReadingList>", path=shelf)


class _FakeKomga:
    """Two Komga read lists; its metadata.number lies (a counter), the file name doesn't."""
    def _get(self, url, params=None):
        if url == "/api/v1/readlists":
            return {"content": [{"id": "rl1", "name": "Witchfinder arc"}, {"id": "rl2", "name": "Empty"}], "last": True}
        if url == "/api/v1/collections":
            return {"content": [{"id": "c1", "name": "Mignolaverse"}, {"id": "c2", "name": "Lonely"}], "last": True}
        if url == "/api/v1/collections/c1/series":
            return {"content": [{"metadata": {"title": "Hellboy: Seed of Destruction"}}, {"metadata": {"title": "B.P.R.D.: Plague of Frogs"}}], "last": True}
        if url == "/api/v1/collections/c2/series":
            return {"content": [{"metadata": {"title": "Only One"}}], "last": True}
        if url == "/api/v1/readlists/rl1/books":
            return {"content": [
                {"seriesTitle": "Sir Edward Grey, Witchfinder", "url": "/data/x/Sir Edward Grey, Witchfinder #002.cbz", "metadata": {"number": "1"}},
                {"seriesTitle": "Hellboy: Seed of Destruction", "url": "/data/x/Hellboy - Seed of Destruction #001.cbz", "metadata": {"number": "7"}},
                {"seriesTitle": "Gone", "url": "/data/x/Gone #001.cbz", "metadata": {"number": "1"}},
            ], "last": True}
        return {"content": [], "last": True}


def test_komga_lists_import_by_file_name(shelf):
    made = rl.import_komga(_FakeKomga(), path=shelf)
    assert [m["name"] for m in made] == ["Witchfinder arc"] and made[0]["entries"] == 3
    r = rl.resolve(made[0]["id"], path=shelf)
    e = r["entries"]
    assert e[0]["status"] == "owned" and e[0]["books"][0]["label"] == "#2"       # file, not Komga's '1'
    assert e[1]["status"] == "owned" and [b["label"] for b in e[1]["books"]] == ["#1"]   # one file, not the whole run
    assert e[2]["status"] == "not_on_shelf"
    assert r["source"] == "komga"
    # importing again replaces, same id
    assert rl.import_komga(_FakeKomga(), path=shelf)[0]["id"] == made[0]["id"]


def test_komga_collections_import_as_series_grain_lists(shelf):
    made = rl.import_komga_collections(_FakeKomga(), path=shelf)
    assert [m["name"] for m in made] == ["Mignolaverse"]          # the one-series collection is no order
    r = rl.resolve(made[0]["id"], path=shelf)
    assert [e["status"] for e in r["entries"]] == ["owned", "owned"]
    assert [b["label"] for b in r["entries"][0]["books"]] == ["#1", "#2"]   # the whole run, in order


def test_renamed_shapes_still_resolve(shelf, tmp_path):
    """Komga (and CBLs) name things the shelf no longer does: a '1 of 4' folder
    that Combine turned into a run, a deluxe edition filed under its run as a
    trade, a '(2020-)' year."""
    root = tmp_path / "comics" / "Dark Horse Comics"
    folder = root / "Hellboy - Seed of Destruction"
    make_cbz(folder / "Hellboy - Seed of Destruction - The Deluxe Edition (2018).cbz")
    sid = db.get_book_by_path(str(folder / "Hellboy - Seed of Destruction #001.cbz"), shelf)["shelf_series_id"]
    db.index_books([(str(folder / "Hellboy - Seed of Destruction - The Deluxe Edition (2018).cbz"), 10, 1.0, None, sid, None)], shelf)
    cbl = b"""<ReadingList><Name>Renamed</Name><Books>
<Book Series="Sir Edward Grey, Witchfinder 2 of 3 - Lost and Gone" Number="1" />
<Book Series="Hellboy: Seed of Destruction - The Deluxe Edition" Number="1" />
<Book Series="Hellboy: Seed of Destruction (1994-)" Number="1" />
<Book Series="Sir Edward Grey, Witchfinder 3 of 3 - The End" Number="1" />
</Books></ReadingList>"""
    lid = rl.import_cbl(cbl, path=shelf)
    e = rl.resolve(lid, path=shelf)["entries"]
    assert e[0]["status"] == "owned" and [b["label"] for b in e[0]["books"]] == ["#2"]
    assert e[1]["status"] == "owned" and e[1]["books"][0]["label"].startswith("Hellboy - Seed of Destruction - The Deluxe")
    assert e[2]["status"] == "owned" and [b["label"] for b in e[2]["books"]] == ["#1", "#2"]
    assert e[3]["status"] == "missing"


def test_covers_for_gaps_come_from_comicvine_then_metron_and_are_remembered(shelf):
    lid = rl.import_cbl(CBL, path=shelf)
    class CV:
        def get_issues_meta(self, ids): return {"218367": {"image_url": "https://cv/seed.jpg"}}
    calls = []
    def search(q): calls.append(q); return [{"id": 9, "title": "Hellboy: The Chained Coffin and Others"}] if "Chained" in q else []
    def issues(sid): return [{"number": 1.0, "image": "https://metron/chained.jpg"}]
    r = rl.fill_covers(lid, path=shelf, cv=CV(), metron_search=search, metron_issues=issues)
    # Seed of Destruction is owned → not asked; Witchfinder #3 (missing) and Chained Coffin (not on shelf) are
    assert r["filled"] == 1 and r["left"] == 0
    e = {x["position"]: x for x in rl.resolve(lid, path=shelf)["entries"]}
    assert e[5]["cover"] and e[5]["cover"].endswith(f"/items/{e[5]['item_id']}/cover") and not e[5]["cover_pending"]
    assert e[4]["cover"] is None and not e[4]["cover_pending"]          # a miss is remembered as ''
    assert rl.fill_covers(lid, path=shelf, cv=CV(), metron_search=search, metron_issues=issues)["filled"] == 0
    assert calls.count("Sir Edward Grey, Witchfinder") == 1              # asked once, not every open


def test_a_one_shot_folded_into_a_run_resolves_by_its_subtitle(shelf, tmp_path):
    """Komga still says 'Batman - One Bad Day - The Riddler'; the shelf has
    'Batman - One Bad Day #001 - The Riddler.cbz' inside the combined run."""
    root = tmp_path / "comics" / "DC Comics"
    folder = root / "Batman - One Bad Day"
    folder.mkdir(parents=True)
    files = ["Batman - One Bad Day #001 - The Riddler (2022).cbz", "Batman - One Bad Day #002 - Two Face (2022).cbz"]
    for f in files:
        make_cbz(folder / f)
    sid = db.upsert_shelf_series(str(folder), "Batman - One Bad Day", "DC Comics", None, 2, "2026-10-09T00:00:00Z", shelf)
    db.index_books([(str(folder / f), 10, 1.0, float(i + 1), sid, None) for i, f in enumerate(files)], shelf)
    cbl = b"""<ReadingList><Name>Bat</Name><Books><Book Series="Batman - One Bad Day - The Riddler" Number="1" />
<Book Series="Batman - One Bad Day - Two Face" Number="1" /><Book Series="Batman - One Bad Day - Nobody" Number="1" /></Books></ReadingList>"""
    e = rl.resolve(rl.import_cbl(cbl, path=shelf), path=shelf)["entries"]
    assert [x["status"] for x in e] == ["owned", "owned", "not_on_shelf"]
    assert [b["label"] for x in e[:2] for b in x["books"]] == ["#1", "#2"]


def test_a_trade_of_a_run_held_whole_is_owned_through_the_run(shelf, tmp_path):
    """'Hellboy in Hell: The Descent' is Vol. 1 of a run the shelf holds 10/10;
    the entry is owned via the run, not sent off to Get. A partial run stays missing."""
    root = tmp_path / "comics" / "Dark Horse Comics"
    tid = db.add_series(title="Hellboy in Hell", publisher="Dark Horse", folder_path=str(root / "Hellboy in Hell"), path=shelf)
    for n in (1, 2):
        db.upsert_issue_status(tid, float(n), "2013-01-01", 1, path=shelf)
    _shelf(shelf, root, "Hellboy in Hell", ["Hellboy in Hell #001.cbz", "Hellboy in Hell #002.cbz"], tracked=tid)
    pid = db.add_series(title="Lobster Johnson", publisher="Dark Horse", folder_path=str(root / "Lobster Johnson"), path=shelf)
    for n in (1, 2, 3):
        db.upsert_issue_status(pid, float(n), "2013-01-01", int(n == 1), path=shelf)
    _shelf(shelf, root, "Lobster Johnson", ["Lobster Johnson #001.cbz"], tracked=pid)
    cbl = b"""<ReadingList><Name>HB</Name><Books><Book Series="Hellboy in Hell: The Descent" Number="1" />
<Book Series="Lobster Johnson: The Burning Hand" Number="1" /></Books></ReadingList>"""
    e = rl.resolve(rl.import_cbl(cbl, path=shelf), path=shelf)["entries"]
    assert e[0]["status"] == "owned" and e[0]["via_run"] == "Hellboy in Hell" and [b["label"] for b in e[0]["books"]] == ["#1", "#2"]
    assert e[1]["status"] == "not_on_shelf"


def test_a_cover_miss_is_retried_after_a_week(shelf):
    lid = rl.import_cbl(CBL, path=shelf)
    calls = []
    def search(q): calls.append(q); return []
    rl.fill_covers(lid, path=shelf, cv=None, metron_search=search, metron_issues=lambda sid: [])
    n = len(calls)
    assert n and rl.fill_covers(lid, path=shelf, cv=None, metron_search=search, metron_issues=lambda sid: [])["left"] == 0
    assert len(calls) == n                                                   # fresh misses aren't re-asked
    with db._connect(shelf) as c:
        c.execute("UPDATE reading_list_items SET cover_checked_at = '2026-01-01T00:00:00Z'")
    rl.fill_covers(lid, path=shelf, cv=None, metron_search=search, metron_issues=lambda sid: [])
    assert len(calls) == 2 * n                                               # a week-old miss is asked again


def test_get_lists_reuses_the_warm_resolution(shelf, monkeypatch):
    """The list cards come from the two-minute cache the Because rows keep; a
    second open resolves nothing."""
    import kometa.related as rel
    lid = rl.import_cbl(CBL, path=shelf)
    rel._lists_cache["value"] = None
    calls = []
    real = rl.resolve
    monkeypatch.setattr(rl, "resolve", lambda list_id, path=None: calls.append(list_id) or real(list_id, path))
    first = rl.get_lists(shelf)
    n = len(calls)
    assert n >= 1 and first[0]["id"] == lid and first[0]["owned"] >= 1
    second = rl.get_lists(shelf)
    assert len(calls) == n and second == first                               # served warm


def test_arc_reading_order_becomes_a_kometa_reading_list(shelf, monkeypatch):
    """The arc page's list button writes a Kometa list (not a Komga readlist):
    entries in reading order, year split off the title, rebuilt in place."""
    import kometa.arcs as arcs
    monkeypatch.setattr(arcs, "DB_PATH", shelf)
    monkeypatch.setattr(rl, "DB_PATH", shelf)
    aid = db.add_series(title="Witchfinder Saga", kind="arc", path=shelf)
    db.replace_arc_reading_order(aid, [
        {"reading_order": 1, "source_title": "Sir Edward Grey, Witchfinder (2012)", "number": "2"},
        {"reading_order": 2, "source_title": "Hellboy - Seed of Destruction", "number": "1"},
        {"reading_order": 3, "source_title": "Nowhere Comics (1999)", "number": "4"},
    ], shelf)
    out = arcs.build_arc_readlist(aid)
    items = rl.get_items(out["list_id"], shelf)
    assert [(i["series"], i["number"], i["year"]) for i in items] == [
        ("Sir Edward Grey, Witchfinder", "2", "2012"), ("Hellboy - Seed of Destruction", "1", None),
        ("Nowhere Comics", "4", "1999")]
    assert out["entries"] == 3 and out["owned"] == 2
    again = arcs.build_arc_readlist(aid)
    assert again["list_id"] == out["list_id"]              # same name = rebuilt in place


def test_a_named_run_year_never_matches_a_different_run_of_the_same_name(db_path, tmp_path):
    """'Fantastic Four' volume 1998 must not resolve to the shelf's Fantastic Four (2018)."""
    root = tmp_path / "comics" / "Marvel Comics"
    t2018 = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=2018, folder_path=str(root / "Fantastic Four"), on_pull_list=False, path=db_path)
    _shelf(db_path, root, "Fantastic Four", ["Fantastic Four #010 (2019).cbz"], tracked=t2018)
    db.upsert_issue_status(t2018, 10.0, "2019-05-01", 1, path=db_path)
    lid = rl.save_list("Doom", [{"series": "Fantastic Four", "number": "500", "volume": "1998"},
                                {"series": "Fantastic Four", "number": "10", "volume": "2018"},
                                {"series": "Fantastic Four", "number": "10"}], "cbl", None, db_path)
    e = rl.resolve(lid, db_path)["entries"]
    assert e[0]["status"] == "not_on_shelf" and e[0]["series_id"] is None     # the 1998 run isn't here: 2018 has no #500
    assert e[1]["status"] == "owned"                                          # the 2018 run is
    assert e[2]["status"] == "owned"                                          # no year: the name decides, as before
