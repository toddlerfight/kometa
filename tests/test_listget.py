"""'Get' on a reading list (kometa/listget.py): track the run without pulling,
queue only what the entry covers, ignore the rest on a series it created."""
import os

import pytest

import kometa.db as db
import kometa.readlists as rl
import kometa.listget as lg
from tests.test_readlists import shelf, CBL  # noqa: F401  (the fixture + the Hellboy-shaped list)


def _metron_search(q):
    rows = {"B.P.R.D.: Plague of Frogs": [{"id": 50058, "title": "B.P.R.D.: Plague of Frogs", "publisher": "Dark Horse Comics", "year": 2005, "issue_count": 4}],
            "Sir Edward Grey, Witchfinder": [{"id": 45124, "title": "Sir Edward Grey, Witchfinder", "publisher": "Dark Horse Comics", "year": 2012, "issue_count": 6}]}
    return rows.get(q.replace(" (2005)", "").replace(" (2012)", ""), [])


def _fake_sync(series):
    n = {"B.P.R.D.: Plague of Frogs": 4, "Sir Edward Grey, Witchfinder": 6}[series["title"]]
    for i in range(1, n + 1):
        db.upsert_issue_status(series["id"], float(i), "2020-01-01", 0, path=db.DB_PATH)


@pytest.fixture
def listdb(shelf, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "DB_PATH", shelf)
    monkeypatch.setattr(lg, "DB_PATH", shelf)
    # the CBL fixture: Seed of Destruction (owned run), Plague of Frogs (BPRD folder, not tracked → owned via shelf),
    # Witchfinder #2 (owned), #3 (missing), Chained Coffin (not on shelf)
    cbl = CBL.replace(b'<Book Series="B.P.R.D.: Plague of Frogs" Number="1" Volume="2005" Year="2005" />',
                      b'<Book Series="B.P.R.D.: Plague of Frogs" Number="1" Volume="2005" Year="2005" />'
                      b'<Book Series="B.P.R.D.: Plague of Frogs" Number="1" Volume="2005" Year="2005" />')
    lid = rl.import_cbl(CBL, path=shelf)
    return {"db": shelf, "lid": lid, "root": str(tmp_path / "comics")}


def test_whole_run_entry_tracks_without_pulling_and_queues_every_issue(listdb, monkeypatch):
    # make Plague of Frogs NOT on the shelf: rename its folder key away
    with db._connect(listdb["db"]) as c:
        c.execute("DELETE FROM books WHERE shelf_series_id IN (SELECT id FROM shelf_series WHERE title = 'BPRD - Plague of Frogs')")
        c.execute("DELETE FROM shelf_series WHERE title = 'BPRD - Plague of Frogs'")
    e = {x["series"]: x for x in rl.resolve(listdb["lid"], listdb["db"])["entries"]}
    assert e["B.P.R.D.: Plague of Frogs"]["status"] == "not_on_shelf"
    r = lg.get_entry(listdb["lid"], e["B.P.R.D.: Plague of Frogs"]["item_id"], listdb["db"],
                     search_metron=_metron_search, search_locg=lambda q: [], sync=_fake_sync, root=listdb["root"])
    assert r["result"] == "queued" and r["created"] and r["queued"] == 4 and r["ignored"] == 0
    s = db.get_series_by_id(r["series_id"], listdb["db"])
    assert s["on_pull_list"] == 0 and s["metron_series_id"] == 50058 and s["from_list_id"] == listdb["lid"]
    assert s["folder_path"].endswith(os.path.join("Dark Horse Comics", "B.P.R.D. - Plague of Frogs")) or "Plague of Frogs" in s["folder_path"]
    q = [x for x in db.get_queue(listdb["db"]) if x["tracked_series_id"] == s["id"]]
    assert sorted(x["issue_number"] for x in q) == [1.0, 2.0, 3.0, 4.0]


def test_volume_entry_on_a_run_the_shelf_has_queues_that_number_only(listdb):
    e = {(x["series"], x["number"]): x for x in rl.resolve(listdb["lid"], listdb["db"])["entries"]}
    # Witchfinder #3 is 'missing': the folder is on the shelf but untracked
    gap = e[("Sir Edward Grey, Witchfinder", "3")]
    assert gap["status"] == "missing" and gap["series_id"] is None
    r = lg.get_entry(listdb["lid"], gap["item_id"], listdb["db"],
                     search_metron=_metron_search, search_locg=lambda q: [], sync=_fake_sync, root=listdb["root"])
    assert r["result"] == "queued" and r["queued"] == 1 and r["created"]
    issues = {i["number"]: i for i in db.get_issues_for_series(r["series_id"], listdb["db"])}
    assert issues[3.0]["ignored"] == 0 and all(issues[n]["ignored"] == 1 for n in (1.0, 2.0, 4.0, 5.0, 6.0))
    q = [x for x in db.get_queue(listdb["db"]) if x["tracked_series_id"] == r["series_id"]]
    assert [x["issue_number"] for x in q] == [3.0]


def test_unknown_run_and_owned_entry(listdb):
    e = {x["series"]: x for x in rl.resolve(listdb["lid"], listdb["db"])["entries"]}
    r = lg.get_entry(listdb["lid"], e["Hellboy: The Chained Coffin and Others"]["item_id"], listdb["db"],
                     search_metron=lambda q: [], search_locg=lambda q: [], sync=_fake_sync, root=listdb["root"])
    # no catalogue knows it → a No-run series of its own + a name-only trade search that waits for confirmation
    assert r["result"] == "proposed_search" and r["created"]
    s = db.get_series_by_id(r["series_id"], listdb["db"])
    assert s["title"] == "Hellboy: The Chained Coffin and Others" and s["metron_link"] == "none" and s["on_pull_list"] == 0
    import json
    q = next(x for x in db.get_queue(listdb["db"]) if x["id"] == r["queue_id"])
    assert q["kind"] == "trade" and json.loads(q["meta_json"])["confirm"] is True and json.loads(q["meta_json"])["year"] == "1998"
    r = lg.get_entry(listdb["lid"], e["Hellboy: Seed of Destruction"]["item_id"], listdb["db"], root=listdb["root"])
    assert r["result"] == "owned"
