"""acquisition.py — the download state machine, run against fakes. No GetComics,
no SABnzbd, no Komga, no network. We seed the DB, inject fake sources, and watch
the queue/issue rows land where they should.

_finalize_usenet_download gets the heaviest coverage here — it moves real files
on disk and was the one extracted function with zero prior exercise.
"""
from datetime import date

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
    """Point acquisition at the temp DB and stub Komga scans + torrent sources
    to no-ops. The torrent stubs matter: _try_torrent runs in the no-source and
    usenet-failed fallback paths, and the real _prowlarr()/_qbittorrent() read
    config via sources.DB_PATH — the container path — which doesn't exist on a
    dev machine, so without these two lines those paths die on 'unable to open
    database file' and mark items failed instead of not_found."""
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "_komga_scan", lambda: None)
    monkeypatch.setattr(acq, "_prowlarr", lambda: None)
    monkeypatch.setattr(acq, "_qbittorrent", lambda: None)
    return db_path, series


class TestProcessQueue:
    def test_getcomics_hit_marks_done_and_owns_issue(self, wired, monkeypatch):
        db_path, series = wired
        # Recent store_date, not incidental: _acquire_issue tries GetComics FIRST
        # only for recent issues (see TestSourceOrderByAge) — an old date here
        # would route through the usenet/torrent-first branch instead, testing
        # something this happy-path test isn't about.
        db.upsert_issue_status(series, 1.0, str(date.today()), owned=False, path=db_path)
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, title, number, store_date, series_year=None, status_fn=None):
                return ("http://dl/saga-1.cbz", "saga-1.cbz")

        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: "/comics/Image/Saga/Saga #001.cbz")

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["owned"] == 1
        # folder_path auto-stamped from the download destination
        assert db.get_series_by_id(series, db_path)["folder_path"] == "/comics/Image/Saga"

    def test_no_source_marks_not_found(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)

        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)   # no usenet client; _prowlarr already None (wired)

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "not_found"


class TestUsenetProgressTracking:
    """A Kometa-initiated SAB download should surface its % through the same
    progress map the queue UI reads, and clear it when the job ends."""

    def _make_pending(self, db_path, series):
        db.queue_pack(series, "nzo1", "http://nzb", db_path)
        return _qid_for(db_path, series, -1.0)

    def test_queued_surfaces_pct(self, wired, monkeypatch):
        db_path, series = wired
        qid = self._make_pending(db_path, series)

        class FakeSab:
            def poll_job(self, nzo):
                return {"status": "queued", "pct": 45.0}
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())

        acq._poll_usenet_jobs()
        assert acq.get_progress(qid) == {"done": 45.0, "total": 100}

    def test_failure_clears_progress(self, wired, monkeypatch):
        db_path, series = wired
        qid = self._make_pending(db_path, series)
        acq.set_progress(qid, 30, 100)

        class FakeSab:
            def poll_job(self, nzo):
                return {"status": "failed", "error": "boom"}
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())

        acq._poll_usenet_jobs()
        assert acq.get_progress(qid) is None
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"


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
        assert issue["owned"] == 1

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


class TestPageMaxOverride:
    """Head Lopper's law: a 72-page quarterly is a real single issue, not a
    webtoon collection. page_max lifts the page-count guard per series; every
    other guard stays armed."""

    def _cbz_with_pages(self, path, n):
        import zipfile
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(n):
                zf.writestr(f"p{i:03d}.jpg", b"x")
        return str(path)

    def test_default_ceiling_rejects_oversized(self, tmp_path):
        from kometa.downloader import _verify_single_issue, WrongIssueError
        cbz = self._cbz_with_pages(tmp_path / "Head Lopper 001.cbz", 72)
        with pytest.raises(WrongIssueError, match="72 pages"):
            _verify_single_issue(cbz, 1.0, "Head Lopper 001.cbz")

    def test_page_max_override_accepts_oversized(self, tmp_path):
        from kometa.downloader import _verify_single_issue
        cbz = self._cbz_with_pages(tmp_path / "Head Lopper 001.cbz", 72)
        _verify_single_issue(cbz, 1.0, "Head Lopper 001.cbz", page_max=150)

    def test_override_ceiling_still_rejects_collections(self, tmp_path):
        from kometa.downloader import _verify_single_issue, WrongIssueError
        cbz = self._cbz_with_pages(tmp_path / "Head Lopper 001.cbz", 300)
        with pytest.raises(WrongIssueError, match="300 pages"):
            _verify_single_issue(cbz, 1.0, "Head Lopper 001.cbz", page_max=150)

    def test_finalize_honors_series_page_max(self, wired, monkeypatch, tmp_path):
        """End-to-end through the usenet finalize: the queue join carries
        s.page_max, and the oversized issue lands instead of failing."""
        db_path, series = wired
        db.set_page_max(series, 150, db_path)
        storage = tmp_path / "sab" / "Head Lopper 001"
        storage.mkdir(parents=True)
        self._cbz_with_pages(storage / "Saga 001.cbz", 72)
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        item = {"id": qid, "issue_number": 1.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series,
                "page_max": db.get_series_by_id(series, db_path)["page_max"]}

        acq._finalize_usenet_download(item, qid, str(storage))

        assert (dest / "Saga #001.cbz").exists()
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"


class TestGetComicsDownloadFallback:
    """A GetComics SEARCH hit doesn't guarantee the linked file host will actually
    serve the file — dead mirror, hotlink block, host-level rate limit (the
    comicfiles.ru wall that ate a whole Detective Comics arc-fulfill batch live was
    exactly this). A download-step failure should fall back to usenet/torrent the
    same as a search-miss does, not hard-fail immediately — except DuplicateIssueError,
    which means 'we probably already have this' and keeps its own 6h-park handling."""

    def test_download_failure_falls_back_to_usenet(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://dead-host/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        def _boom(**kw):
            raise Exception("403 Client Error: Forbidden for url: http://dead-host/saga-1.cbz")
        monkeypatch.setattr(acq.downloader, "download_issue", _boom)

        class FakeSab:
            def add_nzb_url(self, url, nzb_name=None):
                return "nzo123"
        monkeypatch.setattr(acq, "_prowlarr", lambda: object())
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())
        monkeypatch.setattr(acq, "search_usenet", lambda *a, **k: "http://nzb/saga-1.nzb")

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "pending_usenet"
        assert q["sab_nzo_id"] == "nzo123"

    def test_download_failure_with_no_fallback_source_fails(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://dead-host/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        def _boom(**kw):
            raise Exception("403 Client Error: Forbidden")
        monkeypatch.setattr(acq.downloader, "download_issue", _boom)
        # wired fixture stubs _prowlarr/_qbittorrent to None already; _sabnzbd isn't
        # short-circuited away by that (same gotcha test_no_source_marks_not_found
        # hits), so stub it too — otherwise the real accessor tries the container DB.
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert "403" in q["error"]

    def test_duplicate_issue_error_parks_instead_of_falling_back(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://host/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        from kometa.downloader import DuplicateIssueError

        def _dupe(**kw):
            raise DuplicateIssueError("already exists")
        monkeypatch.setattr(acq.downloader, "download_issue", _dupe)
        # No usenet/torrent stubs — if fallback were (wrongly) attempted with the
        # wired fixture's None _prowlarr, it would land 'failed' rather than 'queued'
        # below, so this also proves the fallback path was never entered.

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "queued"          # parked for retry, not failed
        assert q["retry_after"] is not None


class TestSourceOrderByAge:
    """A story-arc 'Get this storyline' fulfill queues a batch of legacy issues —
    exactly the traffic pattern that tipped comicfiles.ru into a Cloudflare wall
    live (8 clean downloads, then blocked for the rest of the session). Old
    issues should try usenet/torrent FIRST; GetComics only as a last resort."""

    def test_is_old_issue_boundary(self):
        from datetime import date, timedelta
        today = date.today()
        assert acq._is_old_issue(None) is False
        assert acq._is_old_issue(str(today)) is False
        assert acq._is_old_issue(str(today - timedelta(days=acq._OLD_ISSUE_DAYS - 1))) is False
        assert acq._is_old_issue(str(today - timedelta(days=acq._OLD_ISSUE_DAYS + 1))) is True

    def test_old_issue_never_touches_getcomics_when_usenet_succeeds(self, wired, monkeypatch):
        db_path, series = wired
        db.upsert_issue_status(series, 1.0, "2012-03-14", owned=False, path=db_path)
        db.queue_issue(series, 1.0, db_path)

        class StrictGC:
            def search(self, *a, **k):
                raise AssertionError("GetComics should not be tried first for an old issue")
        monkeypatch.setattr(acq, "GetComicsClient", StrictGC)

        class FakeSab:
            def add_nzb_url(self, url, nzb_name=None):
                return "nzo1"
        monkeypatch.setattr(acq, "_prowlarr", lambda: object())
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())
        monkeypatch.setattr(acq, "search_usenet", lambda *a, **k: "http://nzb/saga-1.nzb")

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "pending_usenet"

    def test_old_issue_falls_back_to_getcomics_as_last_resort(self, wired, monkeypatch):
        db_path, series = wired
        db.upsert_issue_status(series, 1.0, "2012-03-14", owned=False, path=db_path)
        db.queue_issue(series, 1.0, db_path)
        # wired stubs _prowlarr/_qbittorrent to None already
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://dl/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: "/comics/Image/Saga/Saga #001.cbz")

        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"

    def test_recent_issue_still_tries_getcomics_first(self, wired, monkeypatch):
        db_path, series = wired
        db.upsert_issue_status(series, 1.0, str(date.today()), owned=False, path=db_path)
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://dl/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: "/comics/Image/Saga/Saga #001.cbz")

        # If usenet/torrent were (wrongly) tried first, this would blow up on the
        # wired fixture's unstubbed real _sabnzbd() hitting the container DB path.
        acq._process_queue()

        qid = _qid_for(db_path, series, 1.0)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"


class TestQueuePacing:
    """A multi-item batch paces itself between items (de-bursts the exact
    pattern that trips Cloudflare on a file-host mirror); a lone item doesn't
    pay that cost."""

    def test_no_pace_for_single_item(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)

        slept = []
        monkeypatch.setattr(acq.time, "sleep", lambda s: slept.append(s))

        acq._process_queue()
        assert slept == []

    def test_paces_between_items_in_a_batch(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)
        db.queue_issue(series, 2.0, db_path)

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq, "_sabnzbd", lambda: None)

        slept = []
        monkeypatch.setattr(acq.time, "sleep", lambda s: slept.append(s))

        acq._process_queue()
        assert slept == [2]   # one pause between the two items, none trailing
