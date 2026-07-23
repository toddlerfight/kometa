"""Source-matching tripwires — born 2026-07-02, when the torrent path queued the
Keith Urban 'Ripcord' (2016) ALBUM for issue #0 of the Ripcord (2026) comic.
Name-substring alone hit the acceptance bar, seeders could vault any combined
threshold, and nothing looked at the year. These lock all three doors."""
from datetime import date, timedelta

from kometa.usenet_client import year_mismatch, _nzb_score
from kometa.prowlarr_client import _is_stale, search_torrent, search_usenet


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


def _nzb(title, age, grabs=0, size=120_000_000):
    return {"title": title, "protocol": "usenet", "magnet": "", "url": f"http://nzb/{age}",
            "seeders": 0, "grabs": grabs, "size": size, "age": age, "indexer": "test"}


class TestStaleAgeDemotion:
    """The Absolute Superman #21 grab, act two. The legacy newznab path dropped
    posts older than store_date−45d; the Prowlarr migration never read `age`, so
    a 312-day-old webtoon tied yesterday's print rip on score and won the
    grabs/size coin flip. Staleness now DEMOTES in the sort — it never rejects,
    because sometimes the ancient post is genuinely all that's left."""

    def test_is_stale_reads_prowlarr_age_against_store_date(self):
        store = str(date.today() - timedelta(days=1))
        assert _is_stale(_nzb("x", age=312), store)          # the live offender
        assert not _is_stale(_nzb("x", age=2), store)        # posted with the issue
        assert not _is_stale(_nzb("x", age=30), store)       # early digital leak — fine

    def test_is_stale_boundary_sits_at_the_grace_window(self):
        store = str(date.today())
        assert not _is_stale(_nzb("x", age=60), store)   # exactly grace — not stale
        assert _is_stale(_nzb("x", age=61), store)       # one past — stale

    def test_missing_age_or_store_date_never_flags(self):
        assert not _is_stale(_nzb("x", age=None), str(date.today()))
        assert not _is_stale(_nzb("x", age=312), None)
        assert not _is_stale({"title": "x"}, "not-a-date")   # garbage degrades, no crash

    def test_stale_loses_to_fresh_despite_better_tiebreaks(self):
        # The exact live shape: both score 15, the stale one has more grabs and
        # more bytes — the old sort handed it the win.
        store = str(date.today() - timedelta(days=1))
        stale = _nzb("Absolute Superman 021 [2025] [webtoon-rip]", age=312, grabs=900, size=500_000_000)
        fresh = _nzb("Absolute Superman 021 [2026] [Digital]", age=1, grabs=3)
        p = _FakeProwlarr([stale, fresh])
        assert search_usenet(p, "Absolute Superman", 21.0, store_date=store) == fresh["url"]

    def test_stale_still_wins_when_alone(self):
        # Demotion, not rejection: a back-catalog issue whose only surviving
        # post is ancient must still be grabbable.
        store = str(date.today() - timedelta(days=1))
        stale = _nzb("Absolute Superman 021 [2025] [old-post]", age=312)
        p = _FakeProwlarr([stale])
        assert search_usenet(p, "Absolute Superman", 21.0, store_date=store) == stale["url"]

    def test_no_store_date_degrades_to_the_old_ordering(self):
        # Without a store_date there is nothing to be stale against — the
        # grabs tiebreak decides, exactly as before.
        stale = _nzb("Absolute Superman 021 [2025]", age=312, grabs=900)
        fresh = _nzb("Absolute Superman 021 [2026]", age=1, grabs=3)
        p = _FakeProwlarr([stale, fresh])
        assert search_usenet(p, "Absolute Superman", 21.0, store_date=None) == stale["url"]

    def test_fresh_but_worthless_cannot_shadow_a_stale_real_hit(self):
        # Score bar applies before the stale sort: a fresh result that flunks
        # the evidence gate must not block the stale one that passes it.
        store = str(date.today() - timedelta(days=1))
        junk = _nzb("Something Unrelated Entirely", age=1)
        stale = _nzb("Absolute Superman 021 [2025]", age=312)
        p = _FakeProwlarr([junk, stale])
        assert search_usenet(p, "Absolute Superman", 21.0, store_date=store) == stale["url"]

    def test_torrent_path_demotes_stale_too(self):
        store = str(date.today() - timedelta(days=1))
        stale = _result("Absolute Superman 021 [2025] [webtoon-rip]", 100)
        stale["age"] = 312
        fresh = _result("Absolute Superman 021 [2026] [Digital]", 3)
        fresh["age"] = 1
        p = _FakeProwlarr([stale, fresh])
        got = search_torrent(p, "Absolute Superman", 21.0, store_date=store)
        assert got is not None and "[2026]" in got["title"]
