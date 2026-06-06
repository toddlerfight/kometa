"""acquisition.py — the download state machine, run against fakes. No GetComics,
no SABnzbd, no Komga, no network. We seed the DB, inject fake sources, and watch
the queue/issue rows land where they should.

_finalize_usenet_download gets the heaviest coverage here — it moves real files
on disk and was the one extracted function with zero prior exercise.
"""
import pytest

import kometa.db as db
import kometa.acquisition as acq


# --- ZIP magic so downloader._fix_extension leaves our .cbz files alone ---
ZIP_MAGIC = b"PK\x03\x04"


def _make_comic(path, content=ZIP_MAGIC):
    path.write_bytes(content)
    return str(path)


def _qid_for(db_path, series_id, number):
    return next(q["id"] for q in db.get_queue(db_path)
               if q["tracked_series_id"] == series_id and q["issue_number"] == number)


@pytest.fixture
def wired(db_path, series, monkeypatch):
    """Point acquisition at the temp DB and stub Komga scans to no-ops."""
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "_komga_scan", lambda: None)
    return db_path, series


class TestProcessQueue:
    def test_getcomics_hit_marks_done_and_owns_issue(self, wired, monkeypatch):
        db_path, series = wired
        db.upsert_issue_status(series, 1.0, "2012-03-14", in_komga=False, path=db_path)
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, title, number, store_date, series_year=None):
                return ("http://dl/saga-1.cbz", "saga-1.cbz")

        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: "/comics/Image/Saga/Saga #001.cbz")

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["in_komga"] == 1
        # folder_path auto-stamped from the download destination
        assert db.get_series_by_id(series, db_path)["folder_path"] == "/comics/Image/Saga"

    def test_no_source_marks_not_found(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)

        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq, "_usenet_indexers", lambda: [])
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "not_found"


class TestFinalizeUsenetDownload:
    """The big one — moves SABnzbd output into the library and marks it done."""

    def test_single_file_moved_renamed_and_owned(self, wired, monkeypatch, tmp_path):
        db_path, series = wired
        storage = tmp_path / "sab" / "Saga 001"
        storage.mkdir(parents=True)
        _make_comic(storage / "Saga 001 (2012) (digital).cbz")
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        item = {"id": qid, "issue_number": 1.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": "2012-03-14",
                "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        moved = dest / "Saga #001.cbz"
        assert moved.exists()
        assert not (storage / "Saga 001 (2012) (digital).cbz").exists()
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["in_komga"] == 1

    def test_multi_file_picks_matching_issue(self, wired, monkeypatch, tmp_path):
        db_path, series = wired
        storage = tmp_path / "sab" / "pack"
        storage.mkdir(parents=True)
        _make_comic(storage / "Saga 001.cbz")
        _make_comic(storage / "Saga 002.cbz")
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 2.0, db_path)
        qid = _qid_for(db_path, series, 2.0)
        item = {"id": qid, "issue_number": 2.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        assert (dest / "Saga #002.cbz").exists()
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"

    def test_multi_file_missing_issue_fails(self, wired, monkeypatch, tmp_path):
        db_path, series = wired
        storage = tmp_path / "sab" / "pack"
        storage.mkdir(parents=True)
        _make_comic(storage / "Saga 001.cbz")
        _make_comic(storage / "Saga 002.cbz")
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 5.0, db_path)
        qid = _qid_for(db_path, series, 5.0)
        item = {"id": qid, "issue_number": 5.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert "didn't contain #5" in q["error"]

    def test_pack_sentinel_moves_all_files(self, wired, monkeypatch, tmp_path):
        db_path, series = wired
        storage = tmp_path / "sab" / "fullpack"
        storage.mkdir(parents=True)
        _make_comic(storage / "Saga 001.cbz")
        _make_comic(storage / "Saga 002.cbz")
        _make_comic(storage / "Saga 003.cbz")
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_pack(series, "nzo123", "http://nzb", db_path)
        qid = _qid_for(db_path, series, -1.0)
        item = {"id": qid, "issue_number": -1, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        placed = sorted(p.name for p in dest.iterdir())
        assert placed == ["Saga 001.cbz", "Saga 002.cbz", "Saga 003.cbz"]
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"

    def test_no_comics_in_storage_fails(self, wired, monkeypatch, tmp_path):
        db_path, series = wired
        storage = tmp_path / "sab" / "empty"
        storage.mkdir(parents=True)
        (storage / "readme.txt").write_text("nothing here")
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        item = {"id": qid, "issue_number": 1.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
