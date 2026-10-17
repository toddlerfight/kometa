"""The issue card's one answer (2026-10-17): the web modal and both apps' sheets read
their header from /locg-details, so every card shows the same facts."""
import kometa.db as db
import kometa.main as main
from kometa import record


def _seed(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(record, "details", lambda issue, path, stored_only=False:
                        {"desc": "A heist.", "credits": [], "source": "metron"})
    sid = db.add_series(title="Saga", publisher="Image", year_began=2012, folder_path="/x/Saga", path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", True, metron_issue_id=11, path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-04-11", False, metron_issue_id=12, path=db_path)
    db.upsert_issue_status(sid, 3.0, None, False, path=db_path)          # no catalogue id at all
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO books (path, size, mtime, page_count, tracked_series_id, number) "
                     "VALUES ('/x/Saga/Saga 001.cbz', 1, 1, 0, ?, 1.0)", (sid,))
    return sid


def test_the_card_answer_carries_its_header(db_path, monkeypatch):
    sid = _seed(db_path, monkeypatch)
    d = main.get_issue_locg_details(sid, 1.0)
    assert d["desc"] == "A heist."
    assert (d["series_title"], d["publisher"], d["year_began"]) == ("Saga", "Image", 2012)
    assert d["owned"] is True and d["ignored"] is False
    assert d["store_date"] == "2012-03-14"
    assert d["book_id"]                                   # the shelf's book, without opening it
    assert "run_status" in d


def test_an_unowned_issue_has_no_book_and_shows_ignored(db_path, monkeypatch):
    sid = _seed(db_path, monkeypatch)
    db.set_issue_ignored(sid, 2.0, True, db_path)
    d = main.get_issue_locg_details(sid, 2.0)
    assert d["owned"] is False and d["ignored"] is True and d["book_id"] is None


def test_an_issue_no_catalogue_knows_still_gets_its_header(db_path, monkeypatch):
    sid = _seed(db_path, monkeypatch)
    asked = []
    monkeypatch.setattr(record, "details", lambda *a, **k: asked.append(1) or {})
    d = main.get_issue_locg_details(sid, 3.0)              # read-local-first is off: nothing to ask
    assert not asked
    assert d["series_title"] == "Saga" and d["desc"] == "" and d["credits"] == []
