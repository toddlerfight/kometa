"""Links fixed at the source (db sweep finding 9; foreign keys stay off by the owner's
choice): removing a series unlinks its books and shelf folder, pruning a folder unlinks
its books, and a real error in remove_series is no longer swallowed."""
import sqlite3

import pytest

import kometa.db as db


def _shelf(db_path, sid, folder="/comics/Image/Saga", stamp="2026-10-18 00:00:00"):
    shelf_id = db.upsert_shelf_series(folder, "Saga", "Image", sid, 2, stamp, db_path)
    db.index_books([(f"{folder}/Saga #001.cbz", 1, 1.0, 1.0, shelf_id, sid),
                    (f"{folder}/Saga #002.cbz", 1, 1.0, 2.0, shelf_id, sid)], db_path)
    return shelf_id


def test_removing_a_series_leaves_nothing_pointing_at_it(db_path, series):
    shelf_id = _shelf(db_path, series)
    db.remove_series(series, db_path)
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM books WHERE tracked_series_id = ?", (series,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM shelf_series WHERE tracked_series_id = ?", (series,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2          # the books stay
    assert [s["id"] for s in db.list_shelf("me", path=db_path)] == [shelf_id]          # back on the untracked shelf


def test_pruning_a_folder_unlinks_its_books_and_keeps_them(db_path, series):
    _shelf(db_path, series, stamp="2026-10-17 00:00:00")
    assert db.prune_shelf("2026-10-18 00:00:00", db_path) == 1
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT shelf_series_id FROM books").fetchall()
    assert len(rows) == 2 and all(r[0] is None for r in rows)


def test_a_real_error_in_remove_series_is_raised_and_a_missing_table_is_not(db_path, series, monkeypatch):
    monkeypatch.setattr(db, "_SERIES_ROWS", db._SERIES_ROWS + ("a_table_this_install_never_made",))
    db.remove_series(series, db_path)                     # missing table: fine
    other = db.add_series(title="Rumble", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    monkeypatch.setattr(db, "_SERIES_ROWS", ("config",))  # exists, but has no tracked_series_id: a typo-shaped bug
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        db.remove_series(other, db_path)
