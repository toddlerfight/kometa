"""naming.py — pure parsers. The bread and butter: get the issue number out of
whatever garbage a filename throws at us, and don't mistake a year for an issue.
"""
from kometa import naming


class TestParseIssueNumber:
    def test_hash_format(self):
        assert naming.parse_issue_number("Saga #001.cbz") == 1.0

    def test_hash_decimal(self):
        assert naming.parse_issue_number("Batman #12.5.cbr") == 12.5

    def test_issue_word_format(self):
        assert naming.parse_issue_number("Saga Issue 7.cbz") == 7.0

    def test_strips_title_then_skips_year(self):
        # "Saga" stripped, "2012" is a year (>=1000) so it's skipped, 005 wins
        assert naming.parse_issue_number("Saga 2012 005.cbz", "Saga") == 5.0

    def test_year_skipped_without_title(self):
        assert naming.parse_issue_number("Saga 2012 005.cbz") == 5.0

    def test_no_number_returns_none(self):
        assert naming.parse_issue_number("cover.jpg") is None

    def test_bare_number_over_1000_is_not_an_issue(self):
        # only a 4-digit number present, treated as a year/noise, not an issue
        assert naming.parse_issue_number("Reprint 2018.cbz") is None


class TestScanFolderNumbers:
    def test_collects_comic_numbers_ignores_other_files(self, tmp_path):
        (tmp_path / "Saga #001.cbz").write_text("x")
        (tmp_path / "Saga #002.cbz").write_text("x")
        (tmp_path / "notes.txt").write_text("x")
        assert naming.scan_folder_numbers(str(tmp_path), "Saga") == {1.0, 2.0}

    def test_missing_folder_returns_empty(self):
        assert naming.scan_folder_numbers("/no/such/dir") == set()


class TestFindIssueFile:
    def test_finds_matching_issue(self, tmp_path):
        target = tmp_path / "Saga #003.cbz"
        target.write_text("x")
        (tmp_path / "Saga #004.cbz").write_text("x")
        assert naming.find_issue_file(str(tmp_path), "Saga", 3.0) == str(target)

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "Saga #003.cbz").write_text("x")
        assert naming.find_issue_file(str(tmp_path), "Saga", 99.0) is None

    def test_bad_folder_returns_none(self):
        assert naming.find_issue_file("", "Saga", 1.0) is None
        assert naming.find_issue_file("/no/such/dir", "Saga", 1.0) is None


class TestNormalizeUrl:
    def test_adds_scheme(self):
        assert naming.normalize_url("example.com") == "http://example.com"

    def test_keeps_existing_scheme(self):
        assert naming.normalize_url("https://x.com") == "https://x.com"

    def test_empty_stays_empty(self):
        assert naming.normalize_url("  ") == ""


class TestNorm:
    def test_strips_punctuation_and_lowercases(self):
        assert naming.norm("Spider-Man! (2018)") == "spiderman 2018"
