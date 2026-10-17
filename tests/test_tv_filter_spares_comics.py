"""The TV filter (57bb89e) must not catch comics: 'The Seasons 012' is The Seasons #12, a series
on the pull list, and 'The Complete Series' names comic packs. TV releases still carry a video
marker (720p, DVDRip, MKV, 1x10) and are still dropped."""
from kometa.prowlarr_client import drop_not_comics
from kometa.usenet_client import _nzb_score, _pack_score


def _kept(t, size=50_000_000, single=True):
    return bool(drop_not_comics([{"title": t, "size": size, "categories": []}], single_issue=single))


def test_the_seasons_is_a_comic():
    t = "The Seasons 012 (2026) (Digital) (Zone-Empire)"
    assert _kept(t) and _nzb_score(t, "The Seasons", 12) == 15


def test_complete_series_comic_packs_are_comics():
    for t, s, n in (("Y The Last Man - The Complete Series (2002-2008) (Digital)", "Y The Last Man", 60),
                    ("Invincible Complete Series 001-144 (2003-2018)", "Invincible", 144)):
        assert _kept(t, 3_000_000_000, single=False)
        assert _pack_score(t, s, 3_000_000_000, n) > 0


def test_a_tv_box_set_with_its_video_marker_is_still_dropped():
    t = "Buffy the Vampire Slayer The Complete Series DVDRip"
    assert not _kept(t, 30_000_000_000, single=False) and _pack_score(t, "Buffy the Vampire Slayer", 30_000_000_000) == 0
