"""Komga read-history handover (kometa/komga_import.py): exact path mapping,
stale-rejecting writes, dry run writes nothing."""
import os

import pytest

import kometa.db as db
import kometa.komga_import as ki


class FakeKomga:
    def __init__(self, books):
        self.books = books

    def _get(self, path, params=None):
        want = params["read_status"]
        rows = [b for b in self.books if (b["readProgress"]["completed"] if want == "READ" else not b["readProgress"]["completed"])]
        return {"content": rows, "last": True}


def _kb(url, page, completed, when):
    return {"url": url, "readProgress": {"page": page, "completed": completed, "readDate": when}}


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(ki, "DB_PATH", db_path)
    p1, p2 = str(tmp_path / "Saga #001.cbz"), str(tmp_path / "Saga #002.cbz")
    for p in (p1, p2):
        open(p, "wb").write(b"PK")
    db.index_books([(p1, 2, 1.0, 1.0, None, None), (p2, 2, 1.0, 2.0, None, None)], db_path)
    return {"db": db_path, "p1": p1, "p2": p2}


def test_dry_run_maps_but_writes_nothing(lib):
    k = FakeKomga([_kb(lib["p1"], 7, False, "2026-06-01T00:00:00Z"), _kb(lib["p2"], 24, True, "2026-05-01T00:00:00Z"),
                   _kb("/comics/Nope/Nope #001.cbz", 3, False, "2026-05-01T00:00:00Z")])
    r = ki.import_read_history(dry_run=True, komga=k, path=lib["db"])
    assert r["matched"] == 2 and r["in_progress"] == 1 and r["finished"] == 1 and r["unmatched"] == ["/comics/Nope/Nope #001.cbz"]
    assert r["written"] == 0
    assert db.get_progress("me", db.get_book_by_path(lib["p1"], lib["db"])["id"], lib["db"]) is None


def test_apply_writes_and_never_drags_kometa_backwards(lib):
    b1 = db.get_book_by_path(lib["p1"], lib["db"])["id"]
    db.set_progress("me", b1, 20, False, "2026-10-01T00:00:00Z", lib["db"])          # read further in Kometa since
    k = FakeKomga([_kb(lib["p1"], 7, False, "2026-06-01T00:00:00Z"), _kb(lib["p2"], 24, True, "2026-05-01T00:00:00Z")])
    r = ki.import_read_history(dry_run=False, komga=k, path=lib["db"])
    assert r["written"] == 1 and r["kept_ours"] == 1
    assert db.get_progress("me", b1, lib["db"])["page"] == 20
    b2 = db.get_book_by_path(lib["p2"], lib["db"])["id"]
    assert db.get_progress("me", b2, lib["db"]) == {"page": 24, "completed": 1, "updated_at": "2026-05-01T00:00:00Z"}
