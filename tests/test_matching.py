"""Source-matching tripwires — born 2026-07-02, when the torrent path queued the
Keith Urban 'Ripcord' (2016) ALBUM for issue #0 of the Ripcord (2026) comic.
Name-substring alone hit the acceptance bar, seeders could vault any combined
threshold, and nothing looked at the year. These lock all three doors."""
from kometa.usenet_client import year_mismatch, _nzb_score
from kometa.prowlarr_client import search_torrent


class TestYearMismatch:
    def test_the_actual_offender(self):
        assert year_mismatch("Keith Urban - Ripcord  2016", 2026)

    def test_release_year_at_or_after_series_birth_passes(self):
        assert not year_mismatch("Ripcord 001 (2026) (Digital)", 2026)
        assert not year_mismatch("Batman (1940) Complete Collection", 1940)

    def test_no_year_or_no_series_year_passes(self):
        assert not year_mismatch("Ripcord 001", 2026)
        assert not year_mismatch("Keith Urban - Ripcord 2016", None)

    def test_uses_earliest_year_in_ranges(self):
        # A "pack" spanning back past the series' birth is a different thing.
        assert year_mismatch("Ripcord 2016-2026 MEGA pack", 2026)
        assert not year_mismatch("Ripcord 2026-2027 pack", 2026)

    def test_one_year_grace_for_offset_metadata(self):
        # year_began 2026 but rips stamped 2025 (previews, off-by-one metadata).
        assert not year_mismatch("Ripcord 000 (2025)", 2026)


class _FakeProwlarr:
    def __init__(self, results):
        self._results = results

    def search(self, query, protocol=None, limit=100):
        return self._results


def _result(title, seeders):
    return {"title": title, "protocol": "torrent", "magnet": f"magnet:?xt={title}",
            "url": "", "seeders": seeders, "grabs": 0, "size": 120_000_000,
            "age": 1, "indexer": "test"}


class TestSearchTorrentEvidence:
    def test_name_only_match_rejected_even_with_max_seeders(self):
        # The Keith Urban case: name substring, tons of seeders, zero issue-number
        # evidence. Seeders must not be able to buy acceptance.
        p = _FakeProwlarr([_result("Keith Urban - Ripcord 2016", 100)])
        assert search_torrent(p, "Ripcord", 0.0) is None

    def test_year_mismatch_dropped_before_scoring(self):
        p = _FakeProwlarr([_result("Keith Urban - Ripcord 2016 000", 100)])
        assert search_torrent(p, "Ripcord", 0.0, series_year=2026) is None

    def test_real_issue_match_accepted(self):
        p = _FakeProwlarr([_result("Ripcord 000 (2026) (Digital)", 3)])
        got = search_torrent(p, "Ripcord", 0.0, series_year=2026)
        assert got is not None and "Ripcord 000" in got["title"]

    def test_name_plus_number_required_not_just_high_base(self):
        # A name-only hit (base 10) scores 0 under the evidence gate.
        assert _nzb_score("Keith Urban - Ripcord 2016", "Ripcord", 0.0) == 10  # documents the hole the gate closes


class TestMediaNoiseDisqualified:
    """The live 'Ripcord #0' false positives — the real ones the indexers
    returned. Each scored 15 because a stray '0' (from 'AAC2.0' / 'DTS.MA.2.0')
    matched issue #0 AND the name matched. TV/music/ebook markers now disqualify
    them outright, and the number must sit by the series name, not float free."""

    def test_tv_episodes_rejected(self):
        assert _nzb_score("Shazam.S03E03.Ripcord.1080p.BluRay.REMUX.AVC.DTS-HD.MA.2.0", "Ripcord", 0.0) == 0
        assert _nzb_score("Andy.Richter.Controls.the.Universe.S02E05.Relationship.Ripcord.HDTV.720p.AAC2.0.x264", "Ripcord", 0.0) == 0

    def test_music_and_ebook_rejected(self):
        assert _nzb_score("Keith Urban Ripcord CD FLAC 2016 FORSAKEN", "Ripcord", 0.0) == 0
        assert _nzb_score("Keith Urban-Ripcord-24BIT-WEB-FLAC-2016-TiMES", "Ripcord", 4.0) == 0
        assert _nzb_score("Ripcord by Scott Pratt EPUB", "Ripcord", 0.0) == 0

    def test_real_issue_zero_still_scores_full(self):
        assert _nzb_score("Ripcord 000 [2026] [Digital] [DR & Quinch-Empire]", "Ripcord", 0.0) == 15

    def test_webtoon_digital_mobile_rejected(self):
        # The live Absolute Superman #21 grab: a [digital-mobile] Infinite
        # Edition (vertical 800x1280 webtoon) scored 15 as the print issue.
        assert _nzb_score("Absolute Superman 021 [2025] [digital-mobile] [Son of Ultron-Empire]",
                          "Absolute Superman", 21.0) == 0
        # ...while a plain [Digital] print rip still scores full.
        assert _nzb_score("Absolute Superman 021 [2026] [Digital] [Shan-Empire]",
                          "Absolute Superman", 21.0) == 15

    def test_stray_zero_no_longer_buys_the_number_point(self):
        # bare '2 0' with no media markers: name matches (+10) but the number
        # must be next to the series — a floating '0' no longer counts.
        assert _nzb_score("Some Ripcord audio 2 0 bonus", "Ripcord", 0.0) == 10

    def test_wrong_issue_number_not_matched(self):
        assert _nzb_score("Ripcord 001 (2026) (Digital)", "Ripcord", 0.0) == 10   # 001 is #1, not #0
        assert _nzb_score("Ripcord 001 (2026) (Digital)", "Ripcord", 1.0) == 15
