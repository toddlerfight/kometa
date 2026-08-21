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


class TestExtensionSets:
    """The zoo stays dead. Every module imports these two sets; nobody gets to
    grow a private opinion about what a comic file is ever again."""

    def test_pipeline_is_subset_of_owned(self):
        # anything the pipeline can produce/handle must count as owned on disk
        assert naming.PIPELINE_EXTS <= naming.OWNED_EXTS

    def test_owned_covers_the_historic_drift(self):
        # the exact extensions that used to flip between visible and invisible
        # depending on which scanner looked at them (arcs vs series vs sync)
        assert {'.cb7', '.cbt', '.zip', '.rar', '.pdf'} <= naming.OWNED_EXTS

    def test_pipeline_excludes_unprocessable(self):
        # the extract/rebuild tooling doesn't speak these — owned-only formats
        assert not {'.cb7', '.cbt', '.pdf'} & naming.PIPELINE_EXTS

    def test_scanners_see_cb7_now(self, tmp_path):
        # a hand-dropped .cb7 counts toward series ownership — this was the bug
        (tmp_path / "Saga #003.cb7").write_bytes(b"x")
        assert naming.scan_folder_numbers(str(tmp_path), "Saga") == {3.0}

    def test_scanners_still_ignore_non_comics(self, tmp_path):
        (tmp_path / "Saga #003.jpg").write_bytes(b"x")
        (tmp_path / "Saga #004.nfo").write_bytes(b"x")
        assert naming.scan_folder_numbers(str(tmp_path), "Saga") == set()


class TestEditionKeywords:
    """A vol digit is not an identity. 'Absolute Vol. 1' and 'Vol. 1 TP' share a
    number and NOTHING else — the keyword set is what tells them apart."""

    def test_plain_trade_is_empty_set(self):
        assert naming.edition_keywords("Transmetropolitan Vol. 1: Back on the Street TP") == frozenset()

    def test_absolute_detected(self):
        assert naming.edition_keywords("Absolute Transmetropolitan Vol. 1 HC") == {"absolute"}

    def test_omnibus_and_deluxe_detected(self):
        assert naming.edition_keywords("Saga Deluxe Omnibus Vol 2") == {"deluxe", "omnibus"}

    def test_volume_entries_carry_names(self, tmp_path):
        (tmp_path / "Transmetropolitan v01 - Back On the Street.cbz").write_bytes(b"x")
        (tmp_path / "Absolute Transmetropolitan v01.cbz").write_bytes(b"x")
        entries = naming.scan_folder_volume_entries(str(tmp_path))
        assert len(entries) == 2
        assert {v for v, _ in entries} == {1}
        # the two vol-1 files remain distinguishable by their edition words
        assert {naming.edition_keywords(n) for _, n in entries} == {frozenset(), frozenset({"absolute"})}
