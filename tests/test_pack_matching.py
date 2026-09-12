from kometa.getcomics_client import _pack_post_matches, _normalize

T = _normalize("New Avengers")

class TestPackPostMatches:
    def test_the_real_hickman_pack(self):
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 33 + Extras (2013-2015)",
                                  issue_count=33, series_year=2013)

    def test_wrong_volume_by_issue_count(self):
        # Bendis vol 2 ran to #64 — a complete-run pack for it is not ours.
        assert not _pack_post_matches(T, "New Avengers Vol. 2 #1 – 64 (2010-2012)",
                                      issue_count=33, series_year=2013)

    def test_wrong_volume_by_year(self):
        # Ewing's 2015 run, 18 issues. Both signals disagree.
        assert not _pack_post_matches(T, "New Avengers Vol. 4 #1 – 18 (2015-2016)",
                                      issue_count=33, series_year=2013)

    def test_trade_volume_range_is_not_an_issue_range(self):
        # 'Vol. 1 – 3' is collected volumes, not issues — must not be read as #1-3.
        assert not _pack_post_matches(T, "New Avengers Vol. 4 – A.I.M. Vol. 1 – 3 (TPB) (2016)",
                                      issue_count=33, series_year=2013)

    def test_partial_run_rejected(self):
        # A pack that doesn't start at #1 isn't the complete run we're replacing.
        assert not _pack_post_matches(T, "New Avengers Vol. 3 #10 – 33 (2014-2015)",
                                      issue_count=33, series_year=2013)

    def test_small_count_drift_tolerated(self):
        # .NOW / point-one issues make our count and the pack's differ slightly.
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 31 + Extras (2013-2015)",
                                  issue_count=33, series_year=2013)

    def test_unrelated_series_rejected(self):
        assert not _pack_post_matches(T, "Ultimate Avengers Vol. 1 – 3 + vs. New Ultimates (2009-2011)",
                                      issue_count=33, series_year=2013)

    def test_no_year_in_post_still_matches_on_range(self):
        assert _pack_post_matches(T, "New Avengers Vol. 3 #1 – 33 + Extras",
                                  issue_count=33, series_year=2013)
