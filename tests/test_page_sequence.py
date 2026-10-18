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
        storage, dest, qid, item = self._setup(db_path, series, tmp_path)
        monkeypatch.setattr(acq, "_try_torrent", lambda it, q: False)
        monkeypatch.setattr(acq, "_gc_rescue", lambda it, q: False)

        acq._finalize_usenet_download(item, qid, str(storage))

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "failed"
        assert "start at 003" in q["error"]


def test_a_cover_named_apart_from_the_numbered_pages_counts_as_the_opening():
    names = [f"Where Monsters Dwell 004-{n:03d}.jpg" for n in range(2, 24)] + ["Where Monsters Dwell 004 - Cover.jpg"]
    assert _page_sequence_problem(names) is None
    truly_cut = [f"Where Monsters Dwell 004-{n:03d}.jpg" for n in range(3, 24)]
    assert "pages start at 003" in _page_sequence_problem(truly_cut)


def test_a_fragment_of_a_few_pages_is_incomplete():
    """Puzzlebox #2 landed as three truncated pages named 002-004: too few to judge by the
    old rule, so it passed. A single issue is never that short."""
    assert "only 3 pages" in _page_sequence_problem(pages("x (2021-) 002-", [2, 3, 4]))
    assert _page_sequence_problem(pages("x-", range(0, 20))) is None


def _jpeg(w=200, h=300):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 30, 200)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def test_a_truncated_page_is_caught_by_decoding(tmp_path):
    from kometa.downloader import _truncated_page_problem
    good = _jpeg()
    cbz = tmp_path / "issue.cbz"
    with zipfile.ZipFile(cbz, "w") as z:
        for i in range(10):
            z.writestr(f"p-{i:03d}.jpg", good)
    assert _truncated_page_problem(str(cbz)) is None
    bad = tmp_path / "bad.cbz"
    with zipfile.ZipFile(bad, "w") as z:
        for i in range(9):
            z.writestr(f"p-{i:03d}.jpg", good)
        z.writestr("p-009.jpg", good[: len(good) // 2])                  # the tail lost its articles
    problem = _truncated_page_problem(str(bad))
    assert problem and "p-009.jpg" in problem
    short = tmp_path / "short.cbz"                                        # five bytes short still has every pixel (The Dirt Beneath the Devil #3)
    with zipfile.ZipFile(short, "w") as z:
        for i in range(9):
            z.writestr(f"p-{i:03d}.jpg", good)
        z.writestr("p-009.jpg", good[:-5])
    assert _truncated_page_problem(str(short)) is None
    with zipfile.ZipFile(tmp_path / "mid.cbz", "w") as z:                # Puzzlebox #4: page 007 of 12 was the cut one — every page is judged
        for i in range(20):
            z.writestr(f"p-{i:03d}.jpg", good[: len(good) // 2] if i == 10 else good)
    problem = _truncated_page_problem(str(tmp_path / "mid.cbz"))
    assert problem and "p-010.jpg" in problem


def test_a_bad_crc_entry_condemns_the_archive(tmp_path):
    """Puzzlebox #5 landed as a .cbz with 14 of 18 entries failing their CRC; zipfile
    raises on read, and 'can't read it' must not mean 'fine'."""
    from kometa.downloader import _truncated_page_problem
    good = _jpeg()
    cbz = tmp_path / "crc.cbz"
    with zipfile.ZipFile(cbz, "w") as z:
        for i in range(10):
            z.writestr(f"p-{i:03d}.jpg", good)
    raw = bytearray(cbz.read_bytes())
    # flip bytes inside the third entry's compressed data (stored, so offset = after its local header)
    with zipfile.ZipFile(cbz) as z:
        info = z.infolist()[2]
        start = info.header_offset + 30 + len(info.filename) + len(info.extra) + 40
    raw[start:start + 8] = b"\x00" * 8
    cbz.write_bytes(bytes(raw))
    problem = _truncated_page_problem(str(cbz))
    assert problem and "p-002.jpg" in problem and "CRC" in problem


def test_a_merged_spread_with_a_letter_on_the_end_covers_both_pages():
    """d'argh-Empire's Dark Ark: 'dark_ark_006_010_011m.jpg' is pages 10 and 11.
    Six complete issues were refused as '010, 011, 020, 021 missing' (2026-10-18)."""
    d = "Dark Ark 006 (2018) (digital) (d'argh-Empire)/"
    pages = [f"dark_ark_006_{n:03d}.jpg" for n in range(1, 35) if n not in (10, 11, 20, 21)]
    names = [d + p for p in pages] + [d + "dark_ark_006_010_011m.jpg", d + "dark_ark_006_020_021m.jpg",
                                      d + "zzdelirium_dargh.png"]
    assert _page_sequence_problem(names) is None
    # the same release really missing a spread is still caught
    gone = [n for n in names if not any(s in n for s in ("020_021m", "_015", "_016", "_017"))]
    assert "5 numbered pages missing (015, 016, 017, 020, 021)" in (_page_sequence_problem(gone) or "")
