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


class TestNormKey:
    def test_collapses_punctuation_runs_to_single_spaces(self):
        assert naming.norm_key("Spider-Man! (2018)") == "spider man 2018"

    def test_spacing_variants_collapse_to_same_key(self):
        # The whole point: ':' vs ' - ' vs '  ' must not change the key.
        assert naming.norm_key("Batman: Gargoyle of Gotham") == naming.norm_key("Batman - Gargoyle  of Gotham")

    def test_none_and_empty_are_empty(self):
        assert naming.norm_key(None) == ""
        assert naming.norm_key("  ") == ""


class TestSafe:
    def test_strips_illegal_chars(self):
        assert naming._safe('Bat:man/Year?One') == "Bat-man-Year-One"

    def test_collapses_and_trims_dashes(self):
        assert naming._safe("--Saga--") == "Saga"


class TestPubKey:
    def test_suffix_variants_collapse(self):
        assert naming._pub_key("Image") == naming._pub_key("Image Comics") == "image"

    def test_strips_noise_words_and_punct(self):
        assert naming._pub_key("DC Comics") == "dc"
        assert naming._pub_key("Marvel Entertainment, LLC") == "marvel"


class TestResolveDir:
    def test_matches_existing_folder_despite_publisher_variation(self, tmp_path):
        existing = tmp_path / "Image Comics" / "Saga"
        existing.mkdir(parents=True)
        # short publisher form still resolves to the existing long-form folder
        assert naming._resolve_dir(str(tmp_path), "Image", "Saga") == str(existing)

    def test_matches_existing_folder_case_insensitive_title(self, tmp_path):
        existing = tmp_path / "Image Comics" / "Saga"
        existing.mkdir(parents=True)
        assert naming._resolve_dir(str(tmp_path), "Image Comics", "saga") == str(existing)

    def test_new_series_reuses_existing_publisher_dir(self, tmp_path):
        (tmp_path / "Image Comics").mkdir()
        # new title lands under the canonical existing publisher dir, not a new "Image/"
        assert naming._resolve_dir(str(tmp_path), "Image", "Nimona") == \
            str(tmp_path / "Image Comics" / "Nimona")

    def test_brand_new_publisher_and_title_computes_safe_path(self, tmp_path):
        assert naming._resolve_dir(str(tmp_path), "Oni Press", "Rick & Morty") == \
            str(tmp_path / "Oni Press" / "Rick & Morty")
