"""Whole-shelf index (docs/reader-spec.md step 2). Untracked folders become shelf
rows only — never tracked series — and the scan lists files without opening them."""
import asyncio
import os

import pytest
from fastapi import HTTPException

import kometa.db as db
import kometa.reader as rd
import kometa.shelf as sh
from tests.test_reader import make_book


@pytest.fixture
def shelf(tmp_path, db_path, monkeypatch):
    for mod in (rd, sh):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    root = tmp_path / "comics"
    pages = [(f"p{i}.jpg", 600, 900) for i in range(1, 6)]
    tracked_dir = root / "Image Comics" / "Saga"
    untracked_dir = root / "DC Comics" / "Batman - Knightfall"
    for d in (tracked_dir, untracked_dir, root / "DC Comics" / ".dupes", root / "DC Comics" / "@eaDir"):
        d.mkdir(parents=True)
    make_book(tracked_dir / "Saga #001.cbz", pages)
    make_book(untracked_dir / "Batman - Knightfall #002.cbz", pages)
    make_book(untracked_dir / "Batman - Knightfall #001.cbz", pages)
    make_book(untracked_dir / "Batman - Knightfall Omnibus.cbz", pages)
    (untracked_dir / "notes.pdf").write_bytes(b"%PDF")          # not readable here
    make_book(root / "DC Comics" / ".dupes" / "x #001.cbz", pages)   # quarantine: skipped
    saga = db.add_series(title="Saga", publisher="Image",
                         folder_path=str(tracked_dir), path=db_path)
    return root, saga


def _untracked(root):
    return {s["title"]: s for s in sh.list_shelf()["series"]}


def test_scan_indexes_every_series_folder_and_links_tracked(shelf):
    root, saga = shelf
    r = sh.scan_shelf(str(root))
    assert (r["series"], r["books"]) == (2, 4)                   # .dupes / @eaDir / pdf skipped
    all_rows = {s["title"]: s for s in db.list_shelf("me", untracked_only=False, path=sh.DB_PATH)}
    assert all_rows["Saga"]["tracked_series_id"] == saga
    assert list(_untracked(root)) == ["Batman - Knightfall"]     # only untracked are listed
    assert db.get_all_series(sh.DB_PATH)[0]["title"] == "Saga"     # never became a tracked series
    assert len(db.get_all_series(sh.DB_PATH)) == 1


def test_detail_orders_issues_then_trades_and_opens_in_reader(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    s = _untracked(root)["Batman - Knightfall"]
    d = sh.shelf_detail(s["id"])
    assert [b["label"] for b in d["books"]] == ["#1", "#2", "Batman - Knightfall Omnibus"]
    assert d["books"][0]["page_count"] is None                   # listed, not opened
    assert asyncio.run(rd.book_detail(d["books"][0]["id"]))["page_count"] == 5   # opening reads pages


def test_rescan_keeps_pages_of_unchanged_files_but_clears_changed_ones(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    b = sh.shelf_detail(_untracked(root)["Batman - Knightfall"]["id"])["books"][0]
    asyncio.run(rd.book_detail(b["id"]))
    sh.scan_shelf(str(root))
    assert db.get_book(b["id"], sh.DB_PATH)["page_count"] == 5
    p = db.get_book(b["id"], sh.DB_PATH)["path"]
    os.utime(p, (1, 1))
    sh.scan_shelf(str(root))
    assert db.get_book(b["id"], sh.DB_PATH)["page_count"] is None   # rescanned on next open


def test_vanished_folder_is_pruned_but_progress_survives(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    kid = _untracked(root)["Batman - Knightfall"]["id"]
    book = sh.shelf_detail(kid)["books"][0]
    db.set_progress("me", book["id"], 3, False, "2026-10-08T01:00:00Z", sh.DB_PATH)
    os.rename(root / "DC Comics" / "Batman - Knightfall", root / "DC Comics" / ".parked")
    sh.scan_shelf(str(root))
    assert "Batman - Knightfall" not in _untracked(root)
    assert db.get_progress("me", book["id"], sh.DB_PATH)["page"] == 3


def test_reading_stats_for_untracked_and_tracked(shelf):
    root, saga = shelf
    sh.scan_shelf(str(root))
    kn = _untracked(root)["Batman - Knightfall"]
    b1, b2, _ = sh.shelf_detail(kn["id"])["books"]
    db.set_progress("me", b1["id"], 5, True, "2026-10-08T01:00:00Z", sh.DB_PATH)
    db.set_progress("me", b2["id"], 2, False, "2026-10-08T02:00:00Z", sh.DB_PATH)
    row = _untracked(root)["Batman - Knightfall"]
    assert (row["read_count"], row["in_progress"], row["last_read"]) == (1, 1, "2026-10-08T02:00:00Z")
    saga_book = asyncio.run(rd.issue_book(saga, 1.0))
    db.set_progress("me", saga_book["id"], 2, False, "2026-10-08T03:00:00Z", sh.DB_PATH)
    assert sh.list_shelf()["tracked_reading"][saga]["in_progress"] == 1


def test_cover_is_page_one_of_the_first_book(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    kid = _untracked(root)["Batman - Knightfall"]["id"]
    resp = sh.shelf_cover(kid)
    assert resp.media_type == "image/jpeg" and len(resp.body) > 100
    first = sh.shelf_detail(kid)["books"][0]
    assert first["page_count"] is None          # a cover doesn't open (register) the book


def test_empty_root_refuses_and_prunes_nothing(shelf, tmp_path):
    root, _ = shelf
    sh.scan_shelf(str(root))
    empty = tmp_path / "dead-mount"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        sh.scan_shelf(str(empty))
    assert "Batman - Knightfall" in _untracked(root)


def test_unknown_shelf_series_404s(shelf):
    with pytest.raises(HTTPException) as e:
        sh.shelf_detail(999)
    assert e.value.status_code == 404


def test_a_restart_skips_the_shelf_walk_when_one_ran_lately(monkeypatch, tmp_path):
    import kometa.shelf as sh
    import kometa.db as db
    p = str(tmp_path / "k.db"); db.init_db(p)
    monkeypatch.setattr(sh, "DB_PATH", p)
    walks, after = [], []
    monkeypatch.setattr(sh, "scan_shelf", lambda *a, **k: walks.append(1))
    import kometa.shelf_import as si, kometa.trash as tr
    monkeypatch.setattr(si, "import_in_background", lambda: after.append("import"))
    monkeypatch.setattr(tr, "purge_safe", lambda: after.append("purge"))
    sh.scan_shelf_safe(skip_if_fresh=True)                       # never scanned: walks
    from datetime import datetime, timezone
    db.set_config({sh.SCANNED_KEY: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}, p)
    sh.scan_shelf_safe(skip_if_fresh=True)                       # fresh: skipped
    sh.scan_shelf_safe()                                         # a sync's walk always runs
    assert walks == [1, 1]
    assert after.count("import") == 3 and after.count("purge") == 3   # new folders and the trash, every time


def test_a_file_gone_from_its_folder_leaves_no_ghost_tile(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    kid = _untracked(root)["Batman - Knightfall"]["id"]
    books = sh.shelf_detail(kid)["books"]
    gone = next(p for p in (root / "DC Comics" / "Batman - Knightfall").iterdir() if p.suffix in (".cbz", ".cbr"))
    gone.unlink()
    sh.scan_shelf(str(root))
    assert len(sh.shelf_detail(kid)["books"]) == len(books) - 1


def test_a_vanished_folders_unread_books_go_and_read_ones_stay(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    kid = _untracked(root)["Batman - Knightfall"]["id"]
    b = sh.shelf_detail(kid)["books"]
    db.set_progress("me", b[0]["id"], 3, False, "2026-10-08T01:00:00Z", sh.DB_PATH)
    import shutil
    shutil.rmtree(root / "DC Comics" / "Batman - Knightfall")
    sh.scan_shelf(str(root))
    with db._connect(sh.DB_PATH) as c:
        left = {r[0]: r[1] for r in c.execute("SELECT id, shelf_series_id FROM books WHERE id IN (%s)"
                                              % ",".join("?" * len(b)), [x["id"] for x in b])}
    assert set(left) == {b[0]["id"]}                     # the read one stays…
    assert left[b[0]["id"]] is None                      # …no longer pointing at the folder that's gone (db sweep 9)


def test_loose_rows_from_long_gone_folders_are_cleared(shelf):
    root, _ = shelf
    sh.scan_shelf(str(root))
    with db._connect(sh.DB_PATH) as c:
        c.execute("INSERT INTO books (path, size, mtime, shelf_series_id) VALUES ('/nowhere/Gone #001.cbz', 1, 1.0, 99999)")
    sh.scan_shelf(str(root))
    with db._connect(sh.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM books WHERE path = '/nowhere/Gone #001.cbz'").fetchone()[0] == 0


def test_loose_rows_in_quarantine_folders_are_cleared(shelf):
    root, _ = shelf
    q = root / "Marvel Comics" / "_misgrabbed" / "New Avengers"; q.mkdir(parents=True)
    f = q / "New Avengers #001.cbz"; f.write_bytes(b"PK")
    sh.scan_shelf(str(root))
    with db._connect(sh.DB_PATH) as c:
        c.execute("INSERT INTO books (path, size, mtime, shelf_series_id) VALUES (?, 1, 1.0, 99999)", (str(f),))
    sh.scan_shelf(str(root))
    with db._connect(sh.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM books WHERE path = ?", (str(f),)).fetchone()[0] == 0
