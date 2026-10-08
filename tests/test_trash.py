"""Needs matching → Remove (kometa/trash.py): the folder goes to _trash, the
series and everything pointing at it are forgotten, Undo brings it back, the
purge deletes only what's old enough."""
import os
import time

import pytest

import kometa.db as db
import kometa.trash as tr
from kometa.shelf_import import PENDING, AUTO


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    root = tmp_path / "comics"
    (root / "Indie House" / "Junk Run").mkdir(parents=True)
    (root / "Indie House" / "Junk Run" / "Junk Run #001.cbz").write_bytes(b"zip")
    return {"db": db_path, "root": str(root), "folder": str(root / "Indie House" / "Junk Run")}


def _series(lib, status=PENDING):
    sid = db.add_series(title="Junk Run", publisher="Indie House", folder_path=lib["folder"],
                        on_pull_list=False, path=lib["db"])
    db.set_match_status(sid, status, lib["db"])
    shelf_id = db.upsert_shelf_series(lib["folder"], "Junk Run", "Indie House", sid, 1, "2026-10-08T00:00:00.000000Z", lib["db"])
    db.index_books([(os.path.join(lib["folder"], "Junk Run #001.cbz"), 3, 1.0, 1.0, shelf_id, sid)], lib["db"])
    book = db.list_shelf("me", untracked_only=False, path=lib["db"])
    return sid


def test_trash_moves_the_folder_and_forgets_everything(lib):
    sid = _series(lib)
    r = tr.trash_series(sid, root=lib["root"])
    assert not os.path.exists(lib["folder"])
    assert os.path.isdir(r["trashed_to"]) and r["trashed_to"].startswith(os.path.join(lib["root"], "_trash"))
    assert open(os.path.join(r["trashed_to"], tr.ORIGIN_FILE)).read() == lib["folder"]
    assert db.get_series_by_id(sid, lib["db"]) is None
    assert db.list_shelf("me", untracked_only=False, path=lib["db"]) == []
    with db._connect(lib["db"]) as c:
        assert c.execute("SELECT count(*) FROM books").fetchone()[0] == 0


def test_only_unmatched_series_can_be_removed(lib):
    sid = _series(lib, status=AUTO)
    with pytest.raises(tr.TrashError, match="unmatched"):
        tr.trash_series(sid, root=lib["root"])
    assert os.path.isdir(lib["folder"])


def test_folder_outside_the_root_is_refused(lib, tmp_path):
    sid = _series(lib)
    elsewhere = tmp_path / "elsewhere" / "Junk Run"
    elsewhere.mkdir(parents=True)
    with db._connect(lib["db"]) as c:
        c.execute("UPDATE tracked_series SET folder_path = ? WHERE id = ?", (str(elsewhere), sid))
    with pytest.raises(tr.TrashError, match="outside"):
        tr.trash_series(sid, root=lib["root"])


def test_restore_puts_it_back(lib):
    sid = _series(lib)
    r = tr.trash_series(sid, root=lib["root"])
    assert tr.restore(r["trashed_to"], root=lib["root"]) == lib["folder"]
    assert os.path.isfile(os.path.join(lib["folder"], "Junk Run #001.cbz"))
    assert not os.path.exists(os.path.join(lib["folder"], tr.ORIGIN_FILE))
    with pytest.raises(tr.TrashError):
        tr.restore(r["trashed_to"], root=lib["root"])        # gone from the bin now


def test_purge_deletes_only_what_is_old_enough(lib):
    sid = _series(lib)
    r = tr.trash_series(sid, root=lib["root"])
    assert tr.purge(root=lib["root"]) == 0                      # fresh: stays
    old = time.time() - (tr.TRASH_DAYS + 1) * 86400
    os.utime(os.path.join(r["trashed_to"], tr.ORIGIN_FILE), (old, old))
    assert tr.purge(root=lib["root"]) == 1
    assert not os.path.exists(r["trashed_to"])
    assert not os.path.exists(os.path.dirname(r["trashed_to"]))   # empty publisher dir swept too
