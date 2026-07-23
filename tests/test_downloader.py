"""downloader.py — filename/number parsing, the archive rebuild seam, and the
webtoon dimension guard. No DB or network involved.

_num_from_filename_broad is the pack-content matcher's last resort (after the
'#N' and ComicInfo.xml checks come up empty), used by _pick_issue_file to find
the right file among several in a usenet/torrent/GetComics pack.
"""
import io
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
