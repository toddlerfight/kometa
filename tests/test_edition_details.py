"""An edition of a one-book run carries that book's details — Joker (2008): the
10th Anniversary and Deluxe Editions sat under the original with a bare card,
no credits, while #1 named the whole team (the owner, 2026-10-30). Offline."""
import kometa.db as db
import kometa.record as rec
from kometa import edition_details as ed, main, trade_community as tc

TEAM = [{"role": "Writer", "name": "Brian Azzarello"}, {"role": "Artist", "name": "Lee Bermejo"}]


def _joker(db_path, issues=(1.0,)):
    rec.ensure_tables(db_path)
    sid = db.add_series(title="Joker (2008)", publisher="DC Comics", folder_path="/comics/DC/Joker (2008)", path=db_path)
    for n in issues:
        db.upsert_issue_status(sid, n, "2008-10-29", True, path=db_path)
        rec.write_issue(sid, n, {"desc": "The Joker is out of Arkham.", "credits": TEAM}, "metron", "full", {}, db_path)
    issue = db.upsert_book("/comics/DC/Joker (2008)/Joker #001 (2008).cbz", 1, 1.0, [{}],
                           tracked_series_id=sid, number=1, path=db_path)
    edition = db.upsert_book("/comics/DC/Joker (2008)/Joker - The Deluxe Edition (2020).cbz", 1, 1.0, [{}],
                             tracked_series_id=sid, path=db_path)
    return sid, issue, edition


def test_an_edition_of_a_one_book_run_carries_its_details(db_path):
    _, _, edition = _joker(db_path)
    d = ed.for_book(edition, db_path)
    assert [c["name"] for c in d["credits"]] == ["Brian Azzarello", "Lee Bermejo"]
    assert d["desc"] == "The Joker is out of Arkham."
    assert d["store_date"] == "2008-10-29"
    assert d["of"] == "Joker (2008) #1"


def test_the_issue_itself_and_a_longer_run_get_none(db_path):
    _, issue, edition = _joker(db_path)
    assert ed.for_book(issue, db_path) is None            # the issue card has its own
    run = db_path + ".run"
    db.init_db(run)
    _, _, trade = _joker(run, issues=(1.0, 2.0))
    assert ed.for_book(trade, run) is None                # a trade of a run collects some of it, not all: no guessing


def test_nothing_stored_is_nothing(db_path):
    rec.ensure_tables(db_path)
    sid = db.add_series(title="Joker (2008)", publisher="DC Comics", folder_path="/x", path=db_path)
    db.upsert_issue_status(sid, 1.0, None, True, path=db_path)
    edition = db.upsert_book("/x/Joker - The Deluxe Edition.cbz", 1, 1.0, [{}], tracked_series_id=sid, path=db_path)
    assert ed.for_book(edition, db_path) is None
    assert ed.for_book(999, db_path) is None


def test_the_book_sheet_route_carries_it(db_path, monkeypatch):
    _, _, edition = _joker(db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(ed, "DB_PATH", db_path)
    monkeypatch.setattr(tc, "DB_PATH", db_path)
    r = main.get_book_community(edition)
    assert r["community"] is None
    assert r["details"]["of"] == "Joker (2008) #1" and len(r["details"]["credits"]) == 2
