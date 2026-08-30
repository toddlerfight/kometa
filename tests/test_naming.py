"""naming.py — pure parsers. The bread and butter: get the issue number out of
whatever garbage a filename throws at us, and don't mistake a year for an issue.
"""
import io
import os
import zipfile

from kometa import naming
from kometa.naming import scan_folder_numbers
from tests.conftest import make_cbz


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
        make_cbz(tmp_path / "Saga #001.cbz")
        make_cbz(tmp_path / "Saga #002.cbz")
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


class TestOwnershipReadability:
    """A file that can't produce a single page is not an issue you own. The 76MB
    truncated CBR that listed ZERO entries counted as owned purely because its
    NAME parsed — so nothing ever tried to fetch that issue again. A hole in the
    library that hides itself."""

    def test_real_cbz_counts(self, tmp_path):
        make_cbz(tmp_path / "Saga #001.cbz")
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}

    def test_empty_archive_does_not_count(self, tmp_path):
        buf = io.BytesIO()
        zipfile.ZipFile(buf, "w").close()
        (tmp_path / "Saga #001.cbz").write_bytes(buf.getvalue())
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_archive_with_no_images_does_not_count(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", b"nope")
        (tmp_path / "Saga #001.cbz").write_bytes(buf.getvalue())
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_truncated_zip_does_not_count(self, tmp_path):
        (tmp_path / "Saga #001.cbz").write_bytes(b"PK\x03\x04 and then nothing")
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_pdf_is_taken_at_its_word(self, tmp_path):
        # We can't cheaply probe a PDF, so we don't pretend to. Fail open.
        (tmp_path / "Saga #001.pdf").write_bytes(b"%PDF-1.4 whatever")
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}

    def test_cbz_that_is_neither_zip_nor_rar_does_not_count(self, tmp_path):
        # A .cbz is a promise to be a zip. Broken promise, not a comic.
        (tmp_path / "Saga #001.cbz").write_bytes(b"\x01\x02\x03\x04 mystery meat")
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_all_nul_file_does_not_count(self, tmp_path):
        # The real one: 76MB of NUL bytes wearing a .cbr, a download that
        # reserved its space and never filled it.
        (tmp_path / "Saga #001.cbr").write_bytes(b"\x00" * 4096)
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_unknown_extension_is_taken_at_its_word(self, tmp_path):
        # .pdf and friends we never claimed to be able to probe.
        (tmp_path / "Saga #001.cbt").write_bytes(b"\x01\x02\x03\x04 tar-ish")
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}

    def test_probe_result_is_refreshed_when_the_file_changes(self, tmp_path):
        # A broken file replaced by a good one must flip to owned — the cache is
        # keyed on size+mtime, not path alone, or a re-download stays invisible.
        p = tmp_path / "Saga #001.cbz"
        p.write_bytes(b"PK\x03\x04 broken")
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()
        make_cbz(p, pages=3)
        os.utime(p, (0, 0))
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}


class TestRarReadability:
    """lsar's exit code judges the ARCHIVE, not its own ability to run. The first
    cut of this guard read non-zero as 'couldn't tell' and failed open — which let
    the zero-page CBR through, the one file the guard existed to catch."""

    def _fake_lsar(self, monkeypatch, stdout, returncode):
        import subprocess as sp

        class R:
            pass

        def fake_run(cmd, **kw):
            r = R()
            r.returncode = returncode
            r.stdout = stdout
            return r

        monkeypatch.setattr(naming.subprocess, "run", fake_run)

    def _rar(self, tmp_path):
        p = tmp_path / "Saga #001.cbr"
        p.write_bytes(b"Rar!\x1a\x07\x00 whatever")
        return p

    def test_damaged_but_listable_rar_counts(self, tmp_path, monkeypatch):
        # rc=1 with real entries: partially damaged, but there are pages in there.
        self._fake_lsar(monkeypatch, b"archive\n001.jpg\n002.jpg\n", 1)
        self._rar(tmp_path)
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}

    def test_rar_with_no_listable_entries_does_not_count(self, tmp_path, monkeypatch):
        self._fake_lsar(monkeypatch, b"archive\n", 1)
        self._rar(tmp_path)
        assert scan_folder_numbers(str(tmp_path), "Saga") == set()

    def test_missing_lsar_fails_open(self, tmp_path, monkeypatch):
        def boom(cmd, **kw):
            raise FileNotFoundError("lsar")

        monkeypatch.setattr(naming.subprocess, "run", boom)
        self._rar(tmp_path)
        assert scan_folder_numbers(str(tmp_path), "Saga") == {1.0}
