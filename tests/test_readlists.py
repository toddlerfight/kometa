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
