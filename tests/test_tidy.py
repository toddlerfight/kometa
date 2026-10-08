"""Tidy (kometa/tidy.py): plan says exactly what would move, apply moves it,
paths in the database follow, progress survives, the unparseable stays put."""
import io
import os
import zipfile

import pytest

import kometa.db as db
import kometa.tidy as tidy
import kometa.metron_client as mc


def _cbz(path, n=2):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(n):
            z.writestr(f"p{i}.jpg", b"\xff\xd8jpg")


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(tidy, "DB_PATH", db_path)
    monkeypatch.setattr(mc, "series_detail", lambda sid: {"title": "Batman: Damned", "year": 2018})
    import kometa.sync as sync
    monkeypatch.setattr(sync, "rescan_owned", lambda s, owned_numbers=None: {})
    folder = tmp_path / "comics" / "DC Comics" / "Batman - Damned"
    folder.mkdir(parents=True)
    _cbz(folder / "Batman - Damned 001 (2018) (Digital) (Zone-Empire).cbz")
    _cbz(folder / "Batman - Damned #002.cbz")
    _cbz(folder / "Batman - Damned 002 (2018) (dupe).cbz")          # collides with #002
    _cbz(folder / "Batman - Damned v01 - TPB (2019).cbz")             # no issue number
    (folder / "Batman - Damned #003 (2019).cbz").write_bytes(b"Rar!\x1a\x07\x01\x00stub")   # RAR in .cbz clothing
    sid = db.add_series(title="Batman - Damned", publisher="DC Comics", folder_path=str(folder),
                        year_began=2018, on_pull_list=False, path=db_path)
    db.set_metron_series_id(sid, 7, db_path)
    db.set_match_status(sid, "auto", db_path)
    db.upsert_issue_status_many([(sid, 1.0, "2018-09-19", True, None, None, None, 26),
                                 (sid, 2.0, "2018-12-12", True, None, None, None, 400),
                                 (sid, 3.0, "2019-06-26", True, None, None, None, 3002)], path=db_path)
    shelf_id = db.upsert_shelf_series(str(folder), "Batman - Damned", "DC Comics", sid, 5, "2026-10-08T00:00:00.000000Z", db_path)
    db.index_books([(str(folder / "Batman - Damned #002.cbz"), 10, 1.0, 2.0, shelf_id, sid)], db_path)
    book = db.get_book_by_path(str(folder / "Batman - Damned #002.cbz"), db_path) if hasattr(db, "get_book_by_path") else None
    return {"db": db_path, "sid": sid, "folder": str(folder), "root": str(tmp_path / "comics")}


def test_plan_names_everything_and_leaves_the_unsure(lib):
    p = tidy.plan(lib["sid"], lib["db"])
    assert p["run_title"] == "Batman: Damned" and p["year"] == 2018
    assert p["target_folder"].endswith("DC Comics/Batman - Damned (2018)")
    kinds = {(o["kind"], o["from"], o["to"]) for o in p["ops"]}
    assert ("rename_folder", lib["folder"], p["target_folder"]) in kinds
    assert ("rename_file", "Batman - Damned 001 (2018) (Digital) (Zone-Empire).cbz", "Batman - Damned #001 (2018).cbz") in kinds
    assert ("convert", "Batman - Damned #003 (2019).cbz", "Batman - Damned #003 (2019).cbz") in kinds   # RAR by magic
    reasons = {l["file"]: l["reason"] for l in p["leave"]}
    assert "TPB" in next(f for f in reasons if "TPB" in f) and "no issue number" in reasons["Batman - Damned v01 - TPB (2019).cbz"]
    assert "duplicate" in reasons["Batman - Damned #002.cbz"] and "duplicate" in reasons["Batman - Damned 002 (2018) (dupe).cbz"]
    assert p["counts"] == {"rename_folder": 1, "rename_file": 1, "convert": 1, "leave": 3}


def test_plan_refuses_a_folder_collision(lib):
    os.makedirs(os.path.join(os.path.dirname(lib["folder"]), "Batman - Damned (2018)"))
    with pytest.raises(tidy.TidyError, match="already exists"):
        tidy.plan(lib["sid"], lib["db"])


def test_apply_moves_files_and_folder_and_the_database_follows(lib, monkeypatch):
    import kometa.downloader as dl
    monkeypatch.setattr(dl, "ensure_cbz", lambda p, *a, **k: p)        # the RAR stub can't really repack
    sid, old = lib["sid"], lib["folder"]
    r = tidy.apply(sid, lib["db"])
    new = os.path.join(os.path.dirname(old), "Batman - Damned (2018)")
    assert r["rename_folder"] == 1 and r["rename_file"] == 1 and os.path.isdir(new) and not os.path.exists(old)
    assert os.path.isfile(os.path.join(new, "Batman - Damned #001 (2018).cbz"))
    assert os.path.isfile(os.path.join(new, "Batman - Damned v01 - TPB (2019).cbz"))       # left alone, moved with folder
    assert any(e["file"].startswith("Batman - Damned #003") for e in r["errors"])             # repack refused → reported
    s = db.get_series_by_id(sid, lib["db"])
    assert s["folder_path"] == new and s["title"] == "Batman: Damned"
    with db._connect(lib["db"]) as c:
        paths = [r[0] for r in c.execute("SELECT path FROM books")]
        assert paths == [os.path.join(new, "Batman - Damned #002.cbz")]                      # book row followed the folder
        assert c.execute("SELECT folder_path FROM shelf_series").fetchone()[0] == new


def test_file_and_folder_names():
    assert tidy.folder_name("Batman: Damned", 2018) == "Batman - Damned (2018)"
    assert tidy.folder_name("Saga", None) == "Saga"
    assert tidy.file_name("Batman & The Joker: The Deadly Duo", 7, 2023, ".cbz") == "Batman & The Joker - The Deadly Duo #007 (2023).cbz"
    assert tidy.file_name("Saga", 34.1, 2016, ".cbz") == "Saga #034.1 (2016).cbz"


def test_folder_year_is_the_shelf_year_not_the_cover_date_year(lib, monkeypatch):
    monkeypatch.setattr(mc, "series_detail", lambda sid: {"title": "Batman: Damned", "year": 2019})  # cover-date year
    p = tidy.plan(lib["sid"], lib["db"])
    assert p["year"] == 2018 and p["target_folder"].endswith("Batman - Damned (2018)")        # #1 shipped 2018
