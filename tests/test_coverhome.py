"""Covers beside the comics (kometa/coverhome.py, enrichment step 2). tmp_path only."""
import json
import os

import kometa.db as db
from kometa import coverhome as ch, images


def _store(db_path, tmp_path, key, data=b"JPEGDATA", url="https://x/y.jpg", source="metron"):
    images.ensure_tables(db_path)
    p = tmp_path / "store" / (key.replace("/", "_") + ".jpg")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO image_record (key, path, url, bytes, fetched_at, source) VALUES (?, ?, ?, ?, '', ?)",
                     (key, str(p), url, len(data), source))


def test_placement():
    assert ch.placement("issue/5/1/main") == (5, os.path.join("001", "main.jpg"))
    assert ch.placement("issue/5/605.1/cv") == (5, os.path.join("605.1", "cv.jpg"))
    assert ch.placement("issue/5/12/variant/m181715") == (5, os.path.join("012", "variant-m181715.jpg"))
    assert ch.placement("series/5") == (5, "series.jpg")
    assert ch.placement("trade/5-l:777") == (5, os.path.join("trades", "5-l_777.jpg"))
    assert ch.placement("issue/5/1/file") is None                 # the comic itself regenerates these
    assert ch.placement("ext/abc") is None and ch.placement("book/9") is None


def test_dry_run_counts_and_writes_nothing(tmp_path, db_path):
    folder = tmp_path / "comics" / "Image Comics" / "Saga"
    folder.mkdir(parents=True)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=str(folder), on_pull_list=False, path=db_path)
    _store(db_path, tmp_path, f"issue/{sid}/1/main")
    _store(db_path, tmp_path, f"issue/{sid}/1/variant/v9", data=b"X" * 10)
    _store(db_path, tmp_path, f"issue/{sid}/1/file")
    _store(db_path, tmp_path, "issue/999/1/main")                  # a series with no folder
    p = ch.plan(db_path)
    s = ch.summary(p)
    assert (s["series_folders"], s["files_to_write"], s["covers_without_a_series_folder"], s["not_for_the_nas"]) == (1, 2, 1, 1)
    assert not (folder / ".kometa").exists()                      # a plan writes nothing


def test_copy_writes_files_and_index_and_is_idempotent(tmp_path, db_path):
    folder = tmp_path / "comics" / "Image Comics" / "Saga"
    folder.mkdir(parents=True)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=str(folder), on_pull_list=False, path=db_path)
    _store(db_path, tmp_path, f"issue/{sid}/1/main", url="https://m/1.jpg")
    _store(db_path, tmp_path, f"series/{sid}", url="https://m/s.jpg")
    done = ch.copy(path=db_path)
    assert done["files"] == 2 and not done["errors"]
    home = folder / ".kometa" / "covers"
    assert (home / "001" / "main.jpg").read_bytes() == b"JPEGDATA"
    idx = json.loads((home / "index.json").read_text())
    assert idx[f"issue/{sid}/1/main"] == {"file": os.path.join("001", "main.jpg"), "url": "https://m/1.jpg",
                                          "source": "metron", "bytes": 8}
    assert not [f for f in os.listdir(home) if f.endswith(".kometa-tmp")]       # no temp leftovers
    again = ch.copy(path=db_path)
    assert again["files"] == 0 and ch.summary(ch.plan(db_path))["already_there"] == 2


def test_the_shelf_scan_never_sees_the_covers_home(tmp_path):
    from kometa import shelf
    assert shelf._skip(".kometa")
