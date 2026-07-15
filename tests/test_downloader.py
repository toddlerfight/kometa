"""downloader.py — pure filename/number parsing, no DB or network involved.

_num_from_filename_broad is the pack-content matcher's last resort (after the
'#N' and ComicInfo.xml checks come up empty), used by _pick_issue_file to find
the right file among several in a usenet/torrent/GetComics pack.
"""
from kometa.downloader import _num_from_filename_broad, _pick_issue_file


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
