from kometa.getcomics_client import _pack_post_matches, _normalize

T = _normalize("New Avengers")

class TestPackPostMatches:
    def test_the_real_hickman_pack(self):
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 33 + Extras (2013-2015)",
                                  last_issue=33, series_year=2013)

    def test_wrong_volume_by_issue_count(self):
        # Bendis vol 2 ran to #64 — a complete-run pack for it is not ours.
        assert not _pack_post_matches(T, "New Avengers Vol. 2 #1 – 64 (2010-2012)",
                                      last_issue=33, series_year=2013)

    def test_wrong_volume_by_year(self):
        # Ewing's 2015 run, 18 issues. Both signals disagree.
        assert not _pack_post_matches(T, "New Avengers Vol. 4 #1 – 18 (2015-2016)",
                                      last_issue=33, series_year=2013)

    def test_trade_volume_range_is_not_an_issue_range(self):
        # 'Vol. 1 – 3' is collected volumes, not issues — must not be read as #1-3.
        assert not _pack_post_matches(T, "New Avengers Vol. 4 – A.I.M. Vol. 1 – 3 (TPB) (2016)",
                                      last_issue=33, series_year=2013)

    def test_partial_run_rejected(self):
        # A pack that doesn't start at #1 isn't the complete run we're replacing.
        assert not _pack_post_matches(T, "New Avengers Vol. 3 #10 – 33 (2014-2015)",
                                      last_issue=33, series_year=2013)

    def test_small_count_drift_tolerated(self):
        # .NOW / point-one issues make our count and the pack's differ slightly.
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 31 + Extras (2013-2015)",
                                  last_issue=33, series_year=2013)

    def test_unrelated_series_rejected(self):
        assert not _pack_post_matches(T, "Ultimate Avengers Vol. 1 – 3 + vs. New Ultimates (2009-2011)",
                                      last_issue=33, series_year=2013)

    def test_no_year_in_post_still_matches_on_range(self):
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 33 + Extras",
                                  last_issue=33, series_year=2013)


class TestPackPostMatchesRealWorld:
    """Calibrated against five packs that actually exist, after the first cut was
    tuned on exactly one (New Avengers, #1-33 against 33 issues, a clean sample
    that hid every assumption baked into it)."""

    AV = _normalize("Avengers")
    SW = _normalize("Secret Wars")

    def test_short_pack_against_last_issue_not_issue_count(self):
        # 'Avengers Vol. 5 #1 - 41': the run ends at #44, but the series carries 46
        # rows because 34.1 and 34.2 exist. Measured against the COUNT it looks 5
        # short and gets rejected; against the last issue number it's off by 3.
        assert _pack_post_matches(self.AV, "Avengers Vol. 5 #1 – 41 (2013-2015)",
                                  last_issue=44, series_year=2012)

    def test_run_starting_at_issue_zero(self):
        assert _pack_post_matches(self.SW, "Secret Wars #0 – 9 + Extras (Ultimate Collection) (2015)",
                                  last_issue=9, series_year=2015)

    def test_earlier_volume_rejected_on_year(self):
        # Avengers vol 4 ran #1-34 in 2010. Close enough on count to sneak past a
        # loose numeric rule; the year range is what actually disqualifies it.
        assert not _pack_post_matches(self.AV, "Avengers Vol. 4 #1 – 34 (2010-2013)",
                                      last_issue=44, series_year=2012)

    def test_genuinely_partial_run_rejected(self):
        assert not _pack_post_matches(self.AV, "Avengers Vol. 5 #1 – 30 (2013-2014)",
                                      last_issue=44, series_year=2012)

    def test_creator_collection_has_no_issue_range(self):
        assert not _pack_post_matches(
            self.AV, "Avengers by Jonathan Hickman – The Complete Collection Vol. 1 – 5 (2020-2024)",
            last_issue=44, series_year=2012)

    def test_original_hickman_pack_still_matches(self):
        assert _pack_post_matches(_normalize("New Avengers"),
                                  "New Avengers Vol. 3 #1 – 33 + Extras (2013-2015)",
                                  last_issue=33, series_year=2013)
