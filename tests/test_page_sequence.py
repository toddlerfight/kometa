"""Page-sequence guard — a release that lost pages keeps the numbers of the ones
it still has. Calibrated on the Curse Words usenet corpses: #10 and #21 started at
page 003, #15 at 004 with half its pages gone, and SAB called all three
'completed'."""
import io
import zipfile

import pytest

from kometa.downloader import (_page_sequence_problem, _verify_single_issue,
                               IncompleteIssueError, UnreadableArchiveError)


def pages(prefix, nums, ext=".jpg"):
    return [f"{prefix}{n:03d}{ext}" for n in nums]


class TestPageSequenceProblem:
    def test_complete_zero_based_run_passes(self):
        assert _page_sequence_problem(pages("Curse Words 010-", range(0, 30))) is None

    def test_complete_one_based_run_passes(self):
        assert _page_sequence_problem([f"{n:02d}.jpg" for n in range(1, 30)]) is None

    def test_missing_cover_is_caught(self):
        # The real #10: 25 pages, 003 onward.
        why = _page_sequence_problem(pages("Curse Words 010-", range(3, 28)))
        assert why and "start at 003" in why

    def test_half_an_issue_is_caught(self):
        why = _page_sequence_problem(pages("Curse Words 015-", range(4, 18)))
        assert why and "start at 004" in why

    def test_interior_hole_is_caught(self):
        nums = [n for n in range(0, 30) if n not in (11, 12, 13, 14)]
        why = _page_sequence_problem(pages("Saga 054-", nums))
        assert why and "4 numbered pages missing" in why

    def test_one_skipped_number_is_not_a_hole(self):
        nums = [n for n in range(0, 30) if n != 17]
        assert _page_sequence_problem(pages("Saga 054-", nums)) is None

    def test_spread_covers_both_pages(self):
        names = pages("Saga 054-", range(0, 12)) + ["Saga 054-012-013.jpg"] + pages("Saga 054-", range(14, 30))
        assert _page_sequence_problem(names) is None

    def test_scanner_tags_and_credits_are_ignored(self):
        names = pages("Curse Words 015-", range(0, 30)) + ["zSoU-Nerd.jpg", "zzz-credits-99.jpg"]
        assert _page_sequence_problem(names) is None

    def test_unnumbered_pages_get_no_verdict(self):
        assert _page_sequence_problem([f"page-{c}.jpg" for c in "abcdefghijkl"]) is None

    def test_too_few_pages_to_judge(self):
        assert _page_sequence_problem(pages("x-", range(5, 10))) is None

    def test_folder_prefixes_and_non_images_are_ignored(self):
        names = [f"Curse Words 010/{n}" for n in pages("Curse Words 010-", range(0, 30))]
        names += ["Curse Words 010/ComicInfo.xml", "Curse Words 010/"]
        assert _page_sequence_problem(names) is None

    def test_page_prefix_style(self):
        assert _page_sequence_problem([f"Batman 001 (2016) p{n:02d}.jpg" for n in range(1, 25)]) is None
        assert _page_sequence_problem([f"Batman 001 (2016) p{n:02d}.jpg" for n in range(4, 25)])


def _cbz(path, names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"\xff\xd8\xff\xe0" + b"x" * 400)
    path.write_bytes(buf.getvalue())
    return str(path)


class TestVerifyRejectsIncomplete:
    def test_coverless_issue_raises_incomplete(self, tmp_path):
        p = _cbz(tmp_path / "Curse Words #010.cbz", pages("Curse Words 010-", range(3, 28)))
        with pytest.raises(IncompleteIssueError, match="start at 003"):
            _verify_single_issue(p, 10.0, series_title="Curse Words")

    def test_incomplete_is_a_blacklist_not_a_park(self):
        # _try_getcomics blacklists the link on UnreadableArchiveError and parks
        # 6h on DuplicateIssueError. A holey release must take the first road.
        from kometa.downloader import DuplicateIssueError
        assert issubclass(IncompleteIssueError, UnreadableArchiveError)
        assert not issubclass(IncompleteIssueError, DuplicateIssueError)

    def test_whole_issue_passes(self, tmp_path):
        p = _cbz(tmp_path / "Curse Words #010.cbz", pages("Curse Words 010-", range(0, 30)))
        _verify_single_issue(p, 10.0, series_title="Curse Words")


class TestUsenetFinalizeIncomplete:
    """SAB says 'completed', the file says otherwise. The release gets burned and
    the item falls through to the next source — not stamped done, not dead-ended."""

    def _setup(self, db_path, series, tmp_path):
        import kometa.db as db
        storage = tmp_path / "sab" / "Saga 010"
        storage.mkdir(parents=True)
        _cbz(storage / "Saga 010 (2013) (digital).cbz", pages("Saga 010-", range(3, 28)))
        dest = tmp_path / "lib" / "Saga"
        dest.mkdir(parents=True)
        db.queue_issue(series, 10.0, db_path)
        qid = next(q["id"] for q in db.get_queue(db_path) if q["issue_number"] == 10.0)
        item = {"id": qid, "issue_number": 10.0, "title": "Saga", "publisher": "Image",
                "folder_path": str(dest), "store_date": "2013-03-13", "tracked_series_id": series,
                "source_url": "http://nzb/holey", "failed_channels": "[]"}
        return storage, dest, qid, item

    def test_burns_release_and_falls_through_to_rescue(self, db_path, series, tmp_path, monkeypatch):
        import kometa.acquisition as acq
        import kometa.db as db
        monkeypatch.setattr(acq, "DB_PATH", db_path)
        monkeypatch.setattr(acq, "_komga_scan", lambda: None)
        storage, dest, qid, item = self._setup(db_path, series, tmp_path)
        calls = []
        monkeypatch.setattr(acq, "_try_torrent", lambda it, q: calls.append("torrent") or False)
        monkeypatch.setattr(acq, "_gc_rescue", lambda it, q: calls.append("gc") or True)

        acq._finalize_usenet_download(item, qid, str(storage))

        assert calls == ["torrent", "gc"]
        assert not (dest / "Saga #010.cbz").exists()
        assert not list(storage.iterdir())          # the holey file is gone
        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert "http://nzb/holey" in q["failed_sources"]
        assert "usenet" in q["failed_channels"]

    def test_fails_loudly_when_nothing_else_has_it(self, db_path, series, tmp_path, monkeypatch):
        import kometa.acquisition as acq
        import kometa.db as db
        monkeypatch.setattr(acq, "DB_PATH", db_path)
        monkeypatch.setattr(acq, "_komga_scan", lambda: None)
        storage, dest, qid, item = self._setup(db_path, series, tmp_path)
        monkeypatch.setattr(acq, "_try_torrent", lambda it, q: False)
        monkeypatch.setattr(acq, "_gc_rescue", lambda it, q: False)

        acq._finalize_usenet_download(item, qid, str(storage))

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert "start at 003" in q["error"]
