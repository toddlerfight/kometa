"""On Deck rows (kometa/ondeck.py): continue, next, coming soon — from the
reading tables, in Kometa's issue order, with the labels Kometa can stand behind."""
import os

import pytest

import kometa.db as db
import kometa.ondeck as od


def _book(path_, number, sid, shelf, dbp, pages=24):
    os.makedirs(os.path.dirname(path_), exist_ok=True)
    open(path_, "wb").write(b"PK")
    db.index_books([(path_, 2, 1.0, number, shelf, sid)], dbp)
    b = db.get_book_by_path(path_, dbp)
    with db._connect(dbp) as c:
        c.execute("UPDATE books SET page_count = ? WHERE id = ?", (pages, b["id"]))
    return b["id"]


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(od, "DB_PATH", db_path)
    root = tmp_path / "comics" / "Image"
    saga = db.add_series(title="Saga", publisher="Image", folder_path=str(root / "Saga"), on_pull_list=True, path=db_path)
    low = db.add_series(title="Low", publisher="Image", folder_path=str(root / "Low"), on_pull_list=False, path=db_path)
    sh_saga = db.upsert_shelf_series(str(root / "Saga"), "Saga", "Image", saga, 4, "2026-10-08T00:00:00.000000Z", db_path)
    sh_low = db.upsert_shelf_series(str(root / "Low"), "Low", "Image", low, 2, "2026-10-08T00:00:00.000000Z", db_path)
    books = {}
    for n in (1, 2, 3):
        books[("saga", n)] = _book(str(root / "Saga" / f"Saga #00{n}.cbz"), float(n), saga, sh_saga, db_path)
    books[("saga", "tpb")] = _book(str(root / "Saga" / "Saga v01 TPB.cbz"), None, saga, sh_saga, db_path)
    for n in (1, 2):
        books[("low", n)] = _book(str(root / "Low" / f"Low #00{n}.cbz"), float(n), low, sh_low, db_path)
    db.upsert_issue_status_many([(saga, float(n), "2024-01-01", n <= 3, None, None, None) for n in (1, 2, 3)]
                                + [(saga, 4.0, "2099-01-01", False, None, None, None)], path=db_path)
    db.upsert_issue_status_many([(low, 1.0, "2024-01-01", True, None, None, None), (low, 2.0, "2024-02-01", True, None, None, None),
                                 (low, 3.0, "2024-03-01", False, None, None, None)], path=db_path)
    return {"db": db_path, "saga": saga, "low": low, "b": books}


def test_continue_is_recent_partial_progress_newest_first(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 5, False, "2026-10-01T10:00:00Z", lib["db"])
    db.set_progress("me", b[("low", 1)], 9, False, "2026-10-07T10:00:00Z", lib["db"])
    db.set_progress("me", b[("saga", 2)], 3, False, "2026-01-01T10:00:00Z", lib["db"])        # untouched for months
    rows = od.continue_reading(path=lib["db"])
    assert [(r["series"], r["label"], r["progress"]["page"]) for r in rows] == [("Low", "#1", 9), ("Saga", "#1", 5)]


def test_next_is_the_first_unread_after_the_last_finished_and_skips_trades(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 24, True, "2026-10-01T10:00:00Z", lib["db"])
    db.set_progress("me", b[("saga", 2)], 24, True, "2026-10-02T10:00:00Z", lib["db"])
    rows = od.up_next(path=lib["db"])
    assert [(r["series"], r["label"], r["finished"]) for r in rows] == [("Saga", "#3", 2)]
    # something in progress in that series → it belongs to Continue, not Next
    db.set_progress("me", b[("saga", 3)], 2, False, "2026-10-03T10:00:00Z", lib["db"])
    assert od.up_next(path=lib["db"]) == []


def test_coming_soon_only_when_caught_up_and_labels_from_real_data(lib):
    b = lib["b"]
    for n in (1, 2, 3):
        db.set_progress("me", b[("saga", n)], 24, True, f"2026-10-0{n}T10:00:00Z", lib["db"])
    for n in (1, 2):
        db.set_progress("me", b[("low", n)], 24, True, f"2026-09-0{n}T10:00:00Z", lib["db"])
    rows = {r["series"]: r for r in od.coming_soon(path=lib["db"])}
    assert rows["Saga"]["label"] == "#4" and rows["Saga"]["status"] == "upcoming" and rows["Saga"]["status_label"].startswith("Out 2099")
    assert rows["Low"]["status"] == "not_owned" and "pull list off" in rows["Low"]["status_label"]
    assert list(rows) == ["Saga", "Low"]                                  # most recently read first
