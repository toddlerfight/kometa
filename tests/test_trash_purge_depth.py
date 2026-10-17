"""The trash purge (kometa/trash.py) takes each trashed thing whole, however deep it
sits: a series trashed from straight under the comics root lost its subfolders on the
next purge, with no week of Undo (2026-10-17)."""
import os
import time

import pytest

import kometa.db as db
import kometa.trash as tr
import kometa.twins as twins

OLD = time.time() - (tr.TRASH_DAYS + 1) * 86400


@pytest.fixture
def root(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    return tmp_path / "comics"


def _trash(root, db_path, rel):
    folder = root / rel
    (folder / "Vol 1").mkdir(parents=True)
    (folder / "Vol 1" / "Run #001.cbz").write_bytes(b"zip")
    (folder / "Run #002.cbz").write_bytes(b"zip")
    os.utime(folder / "Vol 1", (OLD, OLD))                     # files are old; the trashing is new
    sid = db.add_series(title=os.path.basename(rel), publisher="X", folder_path=str(folder), on_pull_list=False, path=db_path)
    return tr.trash_series(sid, root=str(root))["trashed_to"]


def test_a_folder_from_straight_under_the_root_keeps_its_whole_week(root, db_path):
    dest = _trash(root, db_path, "Loose Run")
    assert dest == str(root / "_trash" / "Loose Run")
    assert tr.purge(root=str(root)) == 0
    assert os.path.exists(os.path.join(dest, "Vol 1", "Run #001.cbz"))        # its subfolder isn't a trashed series
    assert tr.restore(dest, root=str(root)) == str(root / "Loose Run")        # Undo still has all of it
    assert os.path.exists(root / "Loose Run" / "Vol 1" / "Run #001.cbz")


def test_it_goes_whole_once_its_week_is_up(root, db_path):
    dest = _trash(root, db_path, "Loose Run")
    os.utime(os.path.join(dest, tr.ORIGIN_FILE), (OLD, OLD))
    assert tr.purge(root=str(root)) == 1
    assert not os.path.exists(dest) and os.listdir(root / "_trash") == []


def test_three_deep_is_one_thing_too(root, db_path):
    deep = _trash(root, db_path, os.path.join("Pub", "Imprint", "Deep Run"))
    other = _trash(root, db_path, os.path.join("Pub", "Imprint", "Fresh Run"))
    os.utime(os.path.join(deep, tr.ORIGIN_FILE), (OLD, OLD))
    assert tr.purge(root=str(root)) == 1
    assert not os.path.exists(deep) and os.path.exists(os.path.join(other, "Vol 1", "Run #001.cbz"))


def test_a_file_binned_later_into_the_same_folder_gets_its_own_week(root, db_path):
    run = root / "Pub" / "Run"
    run.mkdir(parents=True)
    for n in ("a", "b"):
        (run / f"{n}.cbz").write_bytes(b"zip")
    twins._bin(str(run / "a.cbz"), str(root), "duplicate", db_path)
    marker = root / "_trash" / "Pub" / "Run" / tr.ORIGIN_FILE
    os.utime(marker, (OLD, OLD))                                             # a.cbz went a week ago
    twins._bin(str(run / "b.cbz"), str(root), "duplicate", db_path)          # b.cbz goes today
    assert tr.purge(root=str(root)) == 0
    assert (root / "_trash" / "Pub" / "Run" / "b.cbz").exists()
