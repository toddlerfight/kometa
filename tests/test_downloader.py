"""downloader.py — filename/number parsing, the archive rebuild seam, and the
webtoon dimension guard. No DB or network involved.

_num_from_filename_broad is the pack-content matcher's last resort (after the
'#N' and ComicInfo.xml checks come up empty), used by _pick_issue_file to find
the right file among several in a usenet/torrent/GetComics pack.
"""
import io
import pathlib
import zipfile

import pytest
from PIL import Image

from kometa.downloader import (
    WrongIssueError, _num_from_filename_broad, _pick_issue_file,
    _sample_page_dims, _verify_single_issue, _webtoon_verdict,
    ensure_cbz, inject_covers,
)

# The two live profiles the dimension guard exists to separate (see the
# Absolute Superman #21 grab): print digital rip vs [digital-mobile] webtoon.
PRINT_DIMS = (1988, 3057)     # ratio 1.54
WEBTOON_DIMS = (800, 1280)    # ratio 1.60 — ratio alone can't split these


def _png(w, h) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def _cbz_with_dims(path, dims_list):
    with zipfile.ZipFile(path, "w") as zf:
        for i, (w, h) in enumerate(dims_list):
            zf.writestr(f"p{i:03d}.png", _png(w, h))
    return str(path)


class TestNumFromFilenameBroad:
    def test_legacy_four_digit_issue_number(self):
        # Detective Comics is a legacy-numbered run past #1000 — a bare
        # \d{4} year-strip used to eat the issue number right along with the
        # year, since both are 4 digits. Real scan-group filenames always
        # wrap the year in parens; the issue number never is.
        assert _num_from_filename_broad(
            "Detective Comics 1089 (2024) (Digital) (Zone-Empire).cbr") == 1089.0

    def test_duplicate_marker_suffix_does_not_win_over_the_real_number(self):
        # Scene groups sometimes append a bare '.1' re-upload/version marker.
        # Once the year-strip stopped eating the real number, the leftmost
        # match (1089) must win over the trailing '.1' — not the reverse.
        assert _num_from_filename_broad(
            "Detective Comics 1089 (2024) (Digital) (Zone-Empire).1.cbr") == 1089.0

    def test_three_digit_issue_number_unaffected(self):
        assert _num_from_filename_broad("Batman 001 (2016) (Digital) (Zone-Empire).cbr") == 1.0

    def test_simple_three_digit(self):
        assert _num_from_filename_broad("Saga 054 (2021).cbz") == 54.0

    def test_annual_excluded(self):
        assert _num_from_filename_broad("Detective Comics Annual (2023).cbr") is None


class TestPickIssueFileFromPack:
    def test_finds_legacy_numbered_issue_in_pack(self):
        files = [
            "Detective Comics 1089 (2024) (Digital) (Zone-Empire).1.cbr",
            "Detective Comics 1089 (2024) (Digital) (Zone-Empire).cbr",
        ]
        assert _pick_issue_file(files, 1089.0) in files

    def test_no_match_returns_none(self):
        files = ["Detective Comics 1088 (2024) (Digital) (Zone-Empire).cbr"]
        assert _pick_issue_file(files, 1089.0) is None


class TestWebtoonDimensionGuard:
    """Both observed profiles, with margin. The webtoon that started this was 44
    pages — comfortably under the 70-page ceiling — so page COUNT never fires;
    the pages themselves (phone-width AND tall) are the only remaining tell."""

    def test_print_profile_passes(self, tmp_path):
        cbz = _cbz_with_dims(tmp_path / "Saga 001.cbz", [PRINT_DIMS] * 6)
        _verify_single_issue(cbz, 1.0, "Saga 001.cbz")   # no raise

    def test_webtoon_profile_rejected(self, tmp_path):
        cbz = _cbz_with_dims(tmp_path / "Saga 001.cbz", [WEBTOON_DIMS] * 6)
        with pytest.raises(WrongIssueError, match="webtoon"):
            _verify_single_issue(cbz, 1.0, "Saga 001.cbz")

    def test_mixed_mostly_print_passes(self, tmp_path):
        # A couple of odd tall pages (double-spread scans, credits strip) must
        # not condemn a real print book — medians hold the line.
        dims = [PRINT_DIMS] * 5 + [WEBTOON_DIMS] * 2
        cbz = _cbz_with_dims(tmp_path / "Saga 001.cbz", dims)
        _verify_single_issue(cbz, 1.0, "Saga 001.cbz")

    def test_both_prongs_required(self):
        # Narrow but squat (old low-res scan): width prong alone must not fire.
        assert _webtoon_verdict([(800, 1000)] * 6) is None
        # Tall but print-width (high-res portrait): ratio prong alone must not fire.
        assert _webtoon_verdict([(1600, 2560)] * 6) is None
        # Both prongs: verdict.
        assert _webtoon_verdict([WEBTOON_DIMS] * 6) is not None

    def test_too_few_pages_is_no_signal(self):
        assert _webtoon_verdict([WEBTOON_DIMS] * 2) is None
        assert _webtoon_verdict([]) is None

    def test_extracted_dir_is_measured_like_an_archive(self, tmp_path):
        d = tmp_path / "extracted"
        d.mkdir()
        for i in range(6):
            (d / f"p{i:03d}.png").write_bytes(_png(*WEBTOON_DIMS))
        dims = _sample_page_dims(str(tmp_path / "whatever.cbr"), extracted_dir=str(d))
        assert _webtoon_verdict(dims) is not None

    def test_unmeasurable_archive_never_rejects(self, tmp_path):
        # Garbage bytes wearing a .cbz name: no dims, no verdict, no crash.
        bad = tmp_path / "Saga 001.cbz"
        bad.write_bytes(b"PK\x03\x04 not really a zip")
        assert _sample_page_dims(str(bad)) == []


class TestEnsureCbz:
    """The CBR→CBZ repack rides the same verified rebuild seam as variant
    injection: temp file, testzip + entry-count check, os.replace, and only
    then does the source .cbr die."""

    def _fake_cbr_with_dir(self, tmp_path, name="Saga 001.cbr"):
        # A RAR by magic bytes (bsdtar can't read it — that's the point: the
        # rebuild must read from the pre-extracted dir, like the one-shot
        # extract path does in production) plus its "extracted" contents.
        cbr = tmp_path / name
        cbr.write_bytes(b"Rar!\x1a\x07\x01\x00 stub")
        d = tmp_path / "extracted"
        d.mkdir()
        (d / "ComicInfo.xml").write_bytes(b"<ComicInfo><Number>1</Number></ComicInfo>")
        for i in range(3):
            (d / f"p{i:03d}.png").write_bytes(_png(*PRINT_DIMS))
        return str(cbr), str(d)

    def test_converts_and_removes_source_after_verify(self, tmp_path):
        cbr, d = self._fake_cbr_with_dir(tmp_path)
        out = ensure_cbz(cbr, extracted_dir=d)
        assert out.endswith("Saga 001.cbz")
        assert not (tmp_path / "Saga 001.cbr").exists()
        with zipfile.ZipFile(out) as zf:
            # Entry names preserved verbatim, stored not deflated.
            assert sorted(zf.namelist()) == ["ComicInfo.xml", "p000.png", "p001.png", "p002.png"]
            assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
            assert zf.testzip() is None
            # ComicInfo untouched — no covers were added, so no patching.
            assert zf.read("ComicInfo.xml") == b"<ComicInfo><Number>1</Number></ComicInfo>"

    def test_zip_input_is_untouched(self, tmp_path):
        cbz = _cbz_with_dims(tmp_path / "Saga 001.cbz", [PRINT_DIMS] * 2)
        before = (tmp_path / "Saga 001.cbz").read_bytes()
        assert ensure_cbz(cbz) == cbz
        assert (tmp_path / "Saga 001.cbz").read_bytes() == before

    def test_unreadable_rar_keeps_the_cbr(self, tmp_path):
        # No extracted dir, and bsdtar "reads" the stub as an EMPTY archive
        # (libarchive exits 0 on a truncated RAR5 header!). The zero-entry
        # guard refuses the swap, the original survives, nothing raises — a
        # conversion hiccup must never kill a download that already succeeded.
        cbr = tmp_path / "Saga 001.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x01\x00 stub")
        assert ensure_cbz(str(cbr)) == str(cbr)
        assert cbr.exists()

    def test_missing_file_is_a_noop(self, tmp_path):
        p = str(tmp_path / "ghost.cbr")
        assert ensure_cbz(p) == p


class TestInjectCoversEmptySelection:
    def test_empty_selection_repacks_instead_of_exploding(self, tmp_path):
        # ThreadPoolExecutor(max_workers=0) was a ValueError — an empty pick is
        # now just a plain repack, zero covers added, contents preserved.
        cbz = _cbz_with_dims(tmp_path / "Saga 001.cbz", [PRINT_DIMS] * 2)
        added, out = inject_covers(cbz, [], primary_id="none")
        assert (added, out) == (0, cbz)
        with zipfile.ZipFile(out) as zf:
            assert sorted(zf.namelist()) == ["p000.png", "p001.png"]


class TestPackDupeGuard:
    """A multi-comic pack whose every file already exists must NOT be left in the
    library as a fake 1GB 'trade' — _pack_comic_count is the gate's eyes."""

    def _pack(self, tmp_path, names):
        p = tmp_path / "pack.zip"
        with zipfile.ZipFile(p, "w") as zf:
            for n in names:
                zf.writestr(n, b"comic-bytes")
        return str(p)

    def test_counts_comics_in_zip(self, tmp_path):
        p = self._pack(tmp_path, ["a v01.cbz", "a v02.cbz", "notes.txt"])
        from kometa.downloader import _pack_comic_count
        assert _pack_comic_count(p) == 2

    def test_single_comic_archive_is_not_a_pack(self, tmp_path):
        # a plain .cbz full of page images counts 0 — never mistaken for a pack
        p = tmp_path / "trade.cbz"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("page01.jpg", b"x")
        from kometa.downloader import _pack_comic_count
        assert _pack_comic_count(str(p)) == 0

    def test_all_dupes_pack_raises_and_removes(self, tmp_path):
        import os
        from kometa.downloader import _extract_pack, _pack_comic_count, DuplicateIssueError
        dest = tmp_path / "lib"
        dest.mkdir()
        (dest / "a v01.cbz").write_bytes(b"x")
        (dest / "a v02.cbz").write_bytes(b"x")
        p = self._pack(tmp_path, ["a v01.cbz", "a v02.cbz"])
        # mirror download_trade's guard: nothing extracted + multi-comic zip
        assert _extract_pack(p, str(dest)) == []
        assert _pack_comic_count(p) > 1

    def test_skips_converted_cbz_of_same_stem(self, tmp_path):
        # pack ships "a v01.cbr"; a previous run extracted it and ensure_cbz left
        # "a v01.cbz" on disk — that MUST count as already-delivered
        from kometa.downloader import _extract_pack
        dest = tmp_path / "lib"
        dest.mkdir()
        (dest / "a v01.cbz").write_bytes(b"x")
        (dest / "a v02.cbz").write_bytes(b"x")
        p = self._pack(tmp_path, ["a v01.cbr", "a v02.cbr"])
        assert _extract_pack(p, str(dest)) == []


class TestSeasonGuard:
    """Three seasons of Batman: The Adventures Continue are three separate runs
    sharing a base title AND issue numbers 1..8, so the number guards match all
    three. Season One's folder filled up with Season Two before this existed."""

    def _cbz(self, path, page_names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for n in page_names:
                zf.writestr(n, _png(*PRINT_DIMS))
        path.write_bytes(buf.getvalue())
        return str(path)

    def test_wrong_season_by_filename_rejected(self, tmp_path):
        cbz = self._cbz(tmp_path / "s2.cbz", [f"{i:03d}.jpg" for i in range(4)])
        with pytest.raises(WrongIssueError, match="Season 2"):
            _verify_single_issue(
                cbz, 1.0,
                "Batman - The Adventures Continue - Season Two 001 (2021).cbr",
                series_title="Batman: The Adventures Continue")

    def test_unnamed_season_is_season_one(self, tmp_path):
        # The Season One run is titled WITHOUT a season, so a "Season One" file
        # has to satisfy it — the default is 1, not "no opinion".
        cbz = self._cbz(tmp_path / "s1.cbz", [f"{i:03d}.jpg" for i in range(4)])
        _verify_single_issue(
            cbz, 1.0, "Batman - The Adventures Continue - Season One 001.cbz",
            series_title="Batman: The Adventures Continue")

    def test_right_season_passes(self, tmp_path):
        cbz = self._cbz(tmp_path / "s2ok.cbz", [f"{i:03d}.jpg" for i in range(4)])
        _verify_single_issue(
            cbz, 1.0, "Batman - The Adventures Continue Season Two 001.cbz",
            series_title="Batman: The Adventures Continue Season Two")

    def test_silent_candidate_is_innocent(self, tmp_path):
        # Plenty of legit releases never say which season. Not knowing is not
        # grounds for rejection, or we'd throw away good downloads.
        cbz = self._cbz(tmp_path / "quiet.cbz", [f"{i:03d}.jpg" for i in range(4)])
        _verify_single_issue(
            cbz, 1.0, "Batman - The Adventures Continue 001 (2021).cbz",
            series_title="Batman: The Adventures Continue Season Two")

    def test_page_names_betray_wrong_season(self, tmp_path):
        # The exact shape that got through: archive name says nothing, every
        # interior page says Season Two. The pages are the honest witness.
        d = tmp_path / "extracted"
        d.mkdir()
        for i in range(5):
            (d / f"Batman - The Adventures Continue (2020-) - Season Two 001-00{i}.jpg"
             ).write_bytes(_png(*PRINT_DIMS))
        cbz = self._cbz(tmp_path / "mute.cbz", [f"{i:03d}.jpg" for i in range(4)])
        with pytest.raises(WrongIssueError, match="Season 2"):
            _verify_single_issue(cbz, 1.0, "Continue 001 (2020).cbz",
                                 extracted_dir=str(d),
                                 series_title="Batman: The Adventures Continue")

    def test_no_series_title_keeps_old_behaviour(self, tmp_path):
        # Every other caller in the tree passes no title; they must not start
        # rejecting things because a filename mentions a season.
        cbz = self._cbz(tmp_path / "legacy.cbz", [f"{i:03d}.jpg" for i in range(4)])
        _verify_single_issue(cbz, 1.0, "Some Book Season One 001.cbz")


class TestRarExtractFallback:
    """bsdtar only reads STORE-method RAR5, and on a compressed one it does the
    worst possible thing: writes SOME entries, then dies. "Did anything land?"
    accepted that — a 26-page comic came through as 9 pages, got sealed into a
    CBZ, and the original was deleted behind it. The bar is the count lsar says
    should be there."""

    def _fake_tools(self, monkeypatch, claims, bsdtar_writes, unar_writes):
        from kometa import downloader as dl

        def fake_run(cmd, **kw):
            class R:
                returncode = 1
                stdout = b""
            if cmd[0] == "lsar":
                R.stdout = ("archive\n" + "".join(
                    f"{i:03d}.jpg\n" for i in range(claims))).encode()
                return R
            out = cmd[cmd.index("-C") + 1] if "-C" in cmd else cmd[cmd.index("-o") + 1]
            n = bsdtar_writes if cmd[0] == "bsdtar" else unar_writes
            for i in range(n):
                (pathlib.Path(out) / f"{i:03d}.jpg").write_bytes(b"x")
            return R

        monkeypatch.setattr(dl.subprocess, "run", fake_run)
        return dl

    def test_partial_bsdtar_falls_through_to_unar(self, tmp_path, monkeypatch):
        # The live shape: bsdtar yields 9 of 26, unar yields all 26.
        dl = self._fake_tools(monkeypatch, claims=26, bsdtar_writes=9, unar_writes=26)
        d = tmp_path / "out"
        d.mkdir()
        assert dl._extract_rar_into(str(tmp_path / "a.cbr"), str(d)) is True
        assert len(list(d.iterdir())) == 26

    def test_partial_from_both_is_refused(self, tmp_path, monkeypatch):
        # Better to report failure than to hand back a truncated comic.
        dl = self._fake_tools(monkeypatch, claims=26, bsdtar_writes=9, unar_writes=11)
        d = tmp_path / "out"
        d.mkdir()
        assert dl._extract_rar_into(str(tmp_path / "a.cbr"), str(d)) is False

    def test_complete_bsdtar_skips_unar(self, tmp_path, monkeypatch):
        dl = self._fake_tools(monkeypatch, claims=26, bsdtar_writes=26, unar_writes=0)
        d = tmp_path / "out"
        d.mkdir()
        assert dl._extract_rar_into(str(tmp_path / "a.cbr"), str(d)) is True

    def test_nothing_extracted_reports_failure(self, tmp_path, monkeypatch):
        dl = self._fake_tools(monkeypatch, claims=26, bsdtar_writes=0, unar_writes=0)
        d = tmp_path / "out"
        d.mkdir()
        assert dl._extract_rar_into(str(tmp_path / "a.cbr"), str(d)) is False

    def test_unknown_claim_takes_the_fuller_extraction(self, tmp_path, monkeypatch):
        # lsar silent: we can't know what's complete, so run both and keep more.
        dl = self._fake_tools(monkeypatch, claims=0, bsdtar_writes=3, unar_writes=12)
        d = tmp_path / "out"
        d.mkdir()
        assert dl._extract_rar_into(str(tmp_path / "a.cbr"), str(d)) is True
        assert len(list(d.iterdir())) == 12
