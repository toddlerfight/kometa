"""acquisition.py — the download state machine, run against fakes. No GetComics,
no SABnzbd, no Komga, no network. We seed the DB, inject fake sources, and watch
the queue/issue rows land where they should.

_finalize_usenet_download gets the heaviest coverage here — it moves real files
on disk and was the one extracted function with zero prior exercise.
"""
import json
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
            def search(self, title, number, store_date, series_year=None, status_fn=None, **k):
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


class TestGCRescueOnUsenetFailure:
    """A failed SAB job on a single issue must try GetComics before dying —
    old issues skip GC on the way in, so the poller is GC's only shot."""

    def _make_pending_issue(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "pending_usenet", path=db_path)
        db.set_sab_nzo_id(qid, "nzo-dead", path=db_path)
        return qid

    def _fail_sab(self, monkeypatch):
        class FakeSab:
            def poll_job(self, nzo):
                return {"status": "failed", "error": "repair impossible"}
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())

    def test_gc_rescue_lands_the_issue(self, wired, monkeypatch):
        db_path, series = wired
        qid = self._make_pending_issue(db_path, series)
        self._fail_sab(monkeypatch)

        class FakeGC:
            def search(self, title, number, store_date, series_year=None, status_fn=None, **k):
                return ("http://dl/saga-1.cbz", "saga-1.cbz")
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)
        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: "/comics/Image/Saga/Saga #001.cbz")

        acq._poll_usenet_jobs()

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["owned"] == 1

    def test_gc_miss_still_fails_with_usenet_error(self, wired, monkeypatch):
        db_path, series = wired
        qid = self._make_pending_issue(db_path, series)
        self._fail_sab(monkeypatch)

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        acq._poll_usenet_jobs()

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert "repair impossible" in q["error"]

    def test_pack_failure_skips_gc_rescue(self, wired, monkeypatch):
        """Packs (issue_number == -1) have no single-issue GC search shape —
        the rescue must stand aside and let the pack fail honestly."""
        db_path, series = wired
        db.queue_pack(series, "nzo-dead", "http://nzb", db_path)
        qid = _qid_for(db_path, series, -1.0)
        self._fail_sab(monkeypatch)

        class ExplodingGC:
            def search(self, *a, **k):
                raise AssertionError("GC rescue must not run for packs")
        monkeypatch.setattr(acq, "GetComicsClient", ExplodingGC)

        acq._poll_usenet_jobs()

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"


class TestFailedSourceBlacklist:
    """A delivery-failed release must be recorded on the row and excluded from
    the next search — retries buy the next-best release, not the same corpse."""

    def test_usenet_failure_records_source_url(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "pending_usenet", source_url="http://nzb/rotten", path=db_path)
        db.set_sab_nzo_id(qid, "nzo-dead", path=db_path)

        class FakeSab:
            def poll_job(self, nzo):
                return {"status": "failed", "error": "repair impossible"}
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        acq._poll_usenet_jobs()

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert acq._failed_sources(q) == {"http://nzb/rotten"}

    def test_add_failed_source_dedupes(self, wired):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.add_failed_source(qid, "http://nzb/a", path=db_path)
        db.add_failed_source(qid, "http://nzb/a", path=db_path)
        db.add_failed_source(qid, "http://nzb/b", path=db_path)
        db.add_failed_source(qid, None, path=db_path)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert acq._failed_sources(q) == {"http://nzb/a", "http://nzb/b"}

    def test_usenet_failure_benches_channel(self, wired, monkeypatch):
        db_path, series = wired
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "pending_usenet", source_url="http://nzb/rotten", path=db_path)
        db.set_sab_nzo_id(qid, "nzo-dead", path=db_path)

        class FakeSab:
            def poll_job(self, nzo):
                return {"status": "failed", "error": "repair impossible"}
        monkeypatch.setattr(acq, "_sabnzbd", lambda: FakeSab())

        class FakeGC:
            def search(self, *a, **k):
                return (None, None)
        monkeypatch.setattr(acq, "GetComicsClient", FakeGC)

        acq._poll_usenet_jobs()

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert acq._failed_channels(q) == {"usenet"}

    def test_benched_usenet_skipped_on_next_attempt(self, wired, monkeypatch):
        """A row whose usenet delivery already died must not touch usenet again —
        the cascade starts at torrent. Prowlarr/SAB are wired LIVE here so the
        usenet rung would genuinely run if the bench didn't hold."""
        db_path, series = wired
        monkeypatch.setattr(acq, "_prowlarr", lambda: object())
        monkeypatch.setattr(acq, "_sabnzbd", lambda: object())
        monkeypatch.setattr(acq, "_prowlarr_on", lambda: True)
        monkeypatch.setattr(acq, "_usenet_on", lambda: True)

        def _boom(prowlarr):
            raise AssertionError("usenet search ran on a benched channel")

        item = {"id": 1, "kind": "issue", "issue_number": 1.0, "title": "Saga",
                "failed_channels": '["usenet"]'}
        # torrent rung: _try_torrent sees _torrent_on unforced → config read would
        # die on the container path; bench torrent too so the call chain stays pure.
        item["failed_channels"] = '["usenet", "torrent"]'
        assert acq._fallback_usenet_torrent(item, 1, _boom, "Saga #1") is False

    def test_benched_torrent_skips_search(self, wired):
        item = {"id": 1, "kind": "issue", "issue_number": 1.0, "title": "Saga",
                "failed_channels": '["torrent"]'}
        assert acq._try_torrent(item, 1) is False

    def test_search_excludes_failed_sources(self):
        from kometa.prowlarr_client import _drop_failed_sources
        results = [{"url": "http://nzb/rotten", "title": "Saga 001"},
                   {"url": "http://nzb/fresh", "title": "Saga 001 (2012)"}]
        kept = _drop_failed_sources(results, {"http://nzb/rotten"})
        assert [r["url"] for r in kept] == ["http://nzb/fresh"]
        # magnet-keyed torrents are excluded by magnet too
        torrents = [{"url": "http://t/x", "magnet": "magnet:?xt=dead", "title": "Saga"}]
        assert _drop_failed_sources(torrents, {"magnet:?xt=dead"}) == []
        # no exclusions = untouched
        assert _drop_failed_sources(results, set()) == results


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


class TestFinalizeWebtoonGuard:
    """The dimension guard, end-to-end through the usenet finalize: a 44-page
    [digital-mobile] webtoon fits under the page-count ceiling, so the pages
    themselves (800x1280 — phone-width, tall) are what must trip the wire."""

    def _cbz_with_dims(self, path, dims, n):
        import io
        import zipfile
        from PIL import Image
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(n):
                buf = io.BytesIO()
                Image.new("RGB", dims, (30, 30, 30)).save(buf, "PNG")
                zf.writestr(f"p{i:03d}.png", buf.getvalue())
        return str(path)

    def _run(self, wired, tmp_path, dims):
        db_path, series = wired
        storage = tmp_path / "sab" / "Saga 001"
        storage.mkdir(parents=True)
        self._cbz_with_dims(storage / "Saga 001.cbz", dims, 8)
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        item = {"id": qid, "issue_number": 1.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series}
        acq._finalize_usenet_download(item, qid, str(storage))
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        return q, storage, dest

    def test_webtoon_parks_with_a_clear_error_and_cleans_the_staging(self, wired, tmp_path):
        q, storage, dest = self._run(wired, tmp_path, (800, 1280))
        assert q["state"] == "failed"
        assert "webtoon" in q["error"]
        # Never shelved — and the rejected file is gone from the job dir too
        # (usenet source is disposable; a torrent's would stay for seeding).
        assert list(dest.iterdir()) == []
        assert not (storage / "Saga 001.cbz").exists()

    def test_print_rip_still_lands(self, wired, tmp_path):
        q, _storage, dest = self._run(wired, tmp_path, (1988, 3057))
        assert q["state"] == "done"
        assert (dest / "Saga #001.cbz").exists()


class TestFinalizeConvertsCbr:
    def test_cbr_lands_as_cbz_and_the_row_tracks_it(self, wired, monkeypatch, tmp_path):
        """No .cbr survives a finalize: the placed file repacks through the
        verified rebuild seam and complete_download records the .cbz — not the
        dead .cbr path. Real RARs need a real extractor, so the one-shot
        extract is stubbed with the 'extracted' pages, exactly the shape
        production hands the seam."""
        import zipfile

        import kometa.downloader as downloader

        db_path, series = wired
        storage = tmp_path / "sab" / "Saga 001"
        storage.mkdir(parents=True)
        (storage / "Saga 001.cbr").write_bytes(b"Rar!\x1a\x07\x01\x00 stub")
        extracted = tmp_path / "rar-extract"
        extracted.mkdir()
        for i in range(3):
            (extracted / f"p{i:03d}.jpg").write_bytes(b"\xff\xd8\xff fakejpeg")
        monkeypatch.setattr(downloader, "_extract_rar_once", lambda p: str(extracted))
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        item = {"id": qid, "issue_number": 1.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": None, "tracked_series_id": series}

        acq._finalize_usenet_download(item, qid, str(storage))

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        assert (dest / "Saga #001.cbz").exists()
        assert not (dest / "Saga #001.cbr").exists()
        assert q["filename"].endswith("Saga #001.cbz")
        with zipfile.ZipFile(dest / "Saga #001.cbz") as zf:
            assert sorted(zf.namelist()) == ["p000.jpg", "p001.jpg", "p002.jpg"]


class TestTradePackDelivered:
    """A range post that lies ('Vol. 1-10' shipping only 1-5) must not mark a
    Vol 9 row done — the pack content check is the only honesty gate."""

    def test_requested_vol_present(self):
        placed = [f"/c/Transmetropolitan v0{i}.cbz" for i in (1, 2, 9)]
        assert acq._trade_pack_delivered(placed, 9, None, {}) is True

    def test_requested_vol_missing(self):
        placed = [f"/c/Transmetropolitan v0{i}.cbz" for i in (1, 2, 3, 4, 5)]
        assert acq._trade_pack_delivered(placed, 9, None, {}) is False

    def test_absolute_request_rejects_plain_files(self):
        placed = ["/c/Transmetropolitan v01.cbz"]
        meta = {"edition_title": "Absolute Transmetropolitan Vol. 1 HC"}
        assert acq._trade_pack_delivered(placed, 1, None, meta) is False

    def test_range_needs_every_volume(self):
        placed = [f"/c/Transmetropolitan v0{i}.cbz" for i in (1, 2, 3)]
        assert acq._trade_pack_delivered(placed, None, [1, 5], {}) is False
        placed = [f"/c/Transmetropolitan v0{i}.cbz" for i in (1, 2, 3, 4, 5)]
        assert acq._trade_pack_delivered(placed, None, [1, 5], {}) is True

    def test_ogn_is_unverifiable_and_trusted(self):
        assert acq._trade_pack_delivered(["/c/Gigs.cbz"], None, None, {}) is True


class TestPackDupCascade:
    """The same lying pack URL matching every row in a batch used to strand all
    the later rows on not_found ('Already downloaded for this series') without
    ever asking usenet or torrents. The whole Transmetropolitan wall, live.
    Now: vol already on disk from the earlier grab → done on the spot; not on
    disk → blacklist the pack for this row and run the cascade like any miss."""

    PACK_URL = "http://dl/transmetro-pack"

    def _trade_series(self, db_path, folder):
        return db.add_series(
            komga_series_id=None, title="Transmetropolitan",
            publisher="DC", year_began=1997, folder_path=str(folder),
            on_pull_list=True, path=db_path,
        )

    def _queue_trade(self, db_path, sid, vol):
        db.queue_trade(sid, f"loc{vol}", "Transmetropolitan", vol=vol,
                       edition_title=f"Transmetropolitan Vol. {vol} TP", path=db_path)
        return next(i for i in db.get_queued_items(db_path)
                    if i.get("kind") == "trade")

    class FakeGC:
        def __init__(self, url):
            self.url = url
            self.seen_excludes = None

        def search_trade(self, title, vol=None, vol_range=None, status_fn=None, exclude_urls=None):
            self.seen_excludes = set(exclude_urls or ())
            if self.url in self.seen_excludes:
                return (None, None)
            return (self.url, None)

    def test_dup_url_with_vol_on_disk_completes(self, wired, monkeypatch, tmp_path):
        db_path, _ = wired
        folder = tmp_path / "Transmetropolitan"
        folder.mkdir()
        _make_comic(folder / "Transmetropolitan v05.cbz")
        sid = self._trade_series(db_path, folder)
        item = self._queue_trade(db_path, sid, 5)

        def _no_download(*a, **k):
            raise AssertionError("must not re-download a pack we already have")
        monkeypatch.setattr(acq.downloader, "download_trade", _no_download)

        acq._acquire_trade(item, item["id"], self.FakeGC(self.PACK_URL), {self.PACK_URL})

        q = next(x for x in db.get_queue(db_path) if x["id"] == item["id"])
        assert q["state"] == "done"

    def test_dup_url_missing_vol_blacklists_and_cascades(self, wired, monkeypatch, tmp_path):
        db_path, _ = wired
        folder = tmp_path / "Transmetropolitan"
        folder.mkdir()
        _make_comic(folder / "Transmetropolitan v05.cbz")   # pack delivered 5, we want 8
        sid = self._trade_series(db_path, folder)
        item = self._queue_trade(db_path, sid, 8)

        fallbacks = []
        monkeypatch.setattr(acq, "_fallback_usenet_torrent",
                            lambda *a, **k: fallbacks.append(a) or False)
        monkeypatch.setattr(acq.downloader, "download_trade",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no re-download")))

        acq._acquire_trade(item, item["id"], self.FakeGC(self.PACK_URL), {self.PACK_URL})

        assert len(fallbacks) == 1, "usenet/torrent cascade must run on a pack dupe"
        q = next(x for x in db.get_queue(db_path) if x["id"] == item["id"])
        assert q["state"] == "not_found"
        assert self.PACK_URL in json.loads(q["failed_sources"])

    def test_blacklisted_pack_is_excluded_on_retry(self, wired, monkeypatch, tmp_path):
        db_path, _ = wired
        folder = tmp_path / "Transmetropolitan"
        folder.mkdir()
        sid = self._trade_series(db_path, folder)
        item = self._queue_trade(db_path, sid, 8)
        db.add_failed_source(item["id"], self.PACK_URL, path=db_path)
        item = next(i for i in db.get_queued_items(db_path) if i["id"] == item["id"])

        gc = self.FakeGC(self.PACK_URL)
        monkeypatch.setattr(acq, "_fallback_usenet_torrent", lambda *a, **k: False)

        acq._acquire_trade(item, item["id"], gc, set())

        assert self.PACK_URL in gc.seen_excludes, "failed_sources must reach search_trade"
        q = next(x for x in db.get_queue(db_path) if x["id"] == item["id"])
        assert q["state"] == "not_found"

    def test_issue_dup_url_cascades_instead_of_dead_ending(self, wired, monkeypatch):
        db_path, series = wired
        db.upsert_issue_status(series, 2.0, str(date.today()), owned=False, path=db_path)
        db.queue_issue(series, 2.0, db_path)
        qid = _qid_for(db_path, series, 2.0)
        item = next(i for i in db.get_queued_items(db_path) if i["id"] == qid)

        class FakeGC:
            def search(self, *a, **k):
                return ("http://dl/pack", "pack.zip")

        monkeypatch.setattr(acq.downloader, "download_issue",
                            lambda **kw: (_ for _ in ()).throw(AssertionError("no re-download")))

        handled, err = acq._try_getcomics(item, qid, FakeGC(), {"http://dl/pack"}, str(date.today()))

        assert (handled, err) == (False, None)
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] != "not_found", "caller owns the final state, not the dup guard"


class TestClientExcludeUrls:
    """GetComicsClient must skip a post whose download link is on the row's
    blacklist — otherwise every retry re-buys the same lying pack in full."""

    def _client(self, monkeypatch, post="http://gc/post", url="http://dl/pack"):
        from kometa.getcomics_client import GetComicsClient
        c = GetComicsClient.__new__(GetComicsClient)   # skip cloudscraper setup
        monkeypatch.setattr(GetComicsClient, "_search_page", lambda *a, **k: post, raising=True)
        monkeypatch.setattr(GetComicsClient, "_search_trade_page", lambda *a, **k: post, raising=True)
        monkeypatch.setattr(GetComicsClient, "_extract_download", lambda *a, **k: (url, "f.cbz"), raising=True)
        return c

    def test_search_skips_excluded(self, monkeypatch):
        c = self._client(monkeypatch)
        assert c.search("Saga", 1.0, exclude_urls={"http://dl/pack"}) == (None, None)
        assert c.search("Saga", 1.0) == ("http://dl/pack", "f.cbz")

    def test_search_trade_skips_excluded(self, monkeypatch):
        c = self._client(monkeypatch)
        assert c.search_trade("Transmetropolitan", vol=8,
                              exclude_urls={"http://dl/pack"}) == (None, None)
        assert c.search_trade("Transmetropolitan", vol=8) == ("http://dl/pack", "f.cbz")
