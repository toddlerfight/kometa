"""The shelf scan sees one level deeper: Batman/Batman (2016)/, Batman
Beyond/Volume 07 (2016)/, Deadpool/Variants/ (345 files it used to miss)."""
import os

import kometa.db as db
import kometa.shelf as shelf


def _cbz(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"PK")


def test_nested_series_folders_become_their_own_shelf_series(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(shelf, "DB_PATH", db_path)
    root = tmp_path / "comics"
    _cbz(str(root / "DC Comics" / "Batman" / "Batman (2016)" / "Batman (2016) #001.cbz"))
    _cbz(str(root / "DC Comics" / "Batman" / "Batman (2016)" / "Batman (2016) #002.cbz"))
    _cbz(str(root / "DC Comics" / "Batman" / "Batman - TPBs" / "Batman - Hush.cbz"))
    _cbz(str(root / "DC Comics" / "Batman Beyond" / "Volume 07 (2016)" / "Batman Beyond #001.cbz"))
    _cbz(str(root / "Marvel Comics" / "Deadpool" / "Deadpool #001.cbz"))            # files AND a sub-folder
    _cbz(str(root / "Marvel Comics" / "Deadpool" / "Variants" / "Deadpool #001 (variant).cbz"))
    _cbz(str(root / "Image Comics" / "Saga" / "Saga #001.cbz"))
    r = shelf.scan_shelf(str(root))
    rows = {s["title"]: s for s in db.list_shelf("me", untracked_only=False, path=db_path)}
    assert set(rows) == {"Batman (2016)", "Batman - TPBs", "Batman Beyond - Volume 07 (2016)", "Deadpool", "Deadpool - Variants", "Saga"}
    assert rows["Batman (2016)"]["book_count"] == 2 and rows["Batman (2016)"]["folder_path"].endswith("Batman/Batman (2016)")
    assert rows["Batman Beyond - Volume 07 (2016)"]["publisher"] == "DC Comics"
    assert r["series"] == 6 and r["books"] == 7
    # the empty parent 'Batman' dir is not a series — nothing to read there
    assert "Batman" not in rows
