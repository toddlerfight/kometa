"""Nightly backup (kometa/backup.py): a consistent DB copy plus a cover mirror, last seven kept."""
import os
import sqlite3

import kometa.backup as bk
import kometa.db as db


def test_backup_copies_the_db_mirrors_covers_and_prunes(db_path, tmp_path):
    covers = tmp_path / "covers" / "issue" / "1" / "1"
    covers.mkdir(parents=True)
    (covers / "main.jpg").write_bytes(b"\xff\xd8" + b"x" * 500)
    dest = tmp_path / "_kometa-backup"
    for i in range(1, 10):                                      # nine stale copies already there
        (dest / f"kometa-2026010{i}.db").parent.mkdir(exist_ok=True)
        (dest / f"kometa-2026010{i}.db").write_bytes(b"old")
    out = bk.backup_now(str(dest), db_path=db_path, covers_dir=str(tmp_path / "covers"))
    assert out["ok"] and out["db_bytes"] > 0 and out["covers_copied"] == 1
    copy = sqlite3.connect(str(dest / out["db_file"]))
    assert copy.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0      # a real database, not bytes
    copy.close()
    kept = sorted(f for f in os.listdir(dest) if f.endswith(".db"))
    assert len(kept) == bk.BACKUP_KEEP and kept[-1] == out["db_file"]
    assert (dest / "covers" / "issue" / "1" / "1" / "main.jpg").stat().st_size == 502
    # a second run copies nothing new and remembers itself
    again = bk.backup_now(str(dest), db_path=db_path, covers_dir=str(tmp_path / "covers"))
    assert again["covers_copied"] == 0 and bk.last(db_path)["db_file"] == again["db_file"]
