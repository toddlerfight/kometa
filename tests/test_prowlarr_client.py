

def test_games_films_and_albums_are_never_a_comic_issue():
    from kometa.prowlarr_client import drop_not_comics
    rs = [
        {"title": "[R.G. Mechanics] The Amazing Spider-Man 2", "size": 7_500_000_000, "categories": [4050]},
        {"title": "Five Nights at Freddy's: Secret of the Mimic v1.004", "size": 2e10, "categories": []},
        {"title": "Hellboy 1, 2, 3 - Action 3 Film Collection 2004-2019", "size": 5.3e9, "categories": [2040]},
        {"title": "[TR24][OF] Trigg & Gusset - Event Horizon (WEB) - 2025 (Dark Jazz, Doom Jazz)", "size": 4e8, "categories": []},
        {"title": "The Amazing Spider-Man 002 (2025) (Digital) (Zone-Empire)", "size": 6e7, "categories": [7030]},
        {"title": "Dark Nights - Death Metal 001 (2020) (Digital)", "size": 9e7, "categories": []},
        {"title": "Sgt. Rock vs. The Army of the Dead 001 (2022)", "size": 5e7, "categories": []},
    ]
    kept = [r["title"] for r in drop_not_comics(rs)]
    assert kept == ["The Amazing Spider-Man 002 (2025) (Digital) (Zone-Empire)",
                    "Dark Nights - Death Metal 001 (2020) (Digital)", "Sgt. Rock vs. The Army of the Dead 001 (2022)"]


def test_pack_drop_not_comics_drops_games_and_movies_but_keeps_large_packs():
    from kometa.prowlarr_client import drop_not_comics
    rs = [
        {"title": "[R.G. Mechanics] Batman Arkham Complete", "size": 15_000_000_000, "categories": [4050]},
        {"title": "Spider-Man Complete Movie Collection 1080p BluRay", "size": 25_000_000_000, "categories": [2040]},
        {"title": "Buffy The Vampire Slayer Complete Soundtrack (FLAC)", "size": 1_200_000_000, "categories": [3010]},
        {"title": "Buffy the Vampire Slayer (001-063) (Digital) Complete", "size": 3_500_000_000, "categories": [7030]},
        {"title": "100 Bullets Complete Collection (Digital)", "size": 4_000_000_000, "categories": []},
    ]
    kept = [r["title"] for r in drop_not_comics(rs, single_issue=False)]
    assert kept == [
        "Buffy the Vampire Slayer (001-063) (Digital) Complete",
        "100 Bullets Complete Collection (Digital)",
    ]


def test_tv_show_releases_are_dropped_for_buffy():
    from kometa.prowlarr_client import drop_not_comics
    from kometa.usenet_client import _nzb_score, _pack_score

    tv_candidates = [
        "Buffy the Vampire Slayer S01E10 720p HDTV",
        "Buffy the Vampire Slayer 1x10 Nightmares",
        "Buffy the Vampire Slayer The Complete Series DVDRip",
        "Buffy the Vampire Slayer Complete Seasons 1-7 (1080p)",
        "Buffy the Vampire Slayer - Season 1 (1997) [MKV]",
    ]

    # drop_not_comics drops every TV show variant
    rs = [{"title": t, "size": 1_000_000_000, "categories": []} for t in tv_candidates]
    assert drop_not_comics(rs, single_issue=False) == []

    # _nzb_score and _pack_score disqualify all TV releases immediately (score 0)
    for t in tv_candidates:
        assert _nzb_score(t, "Buffy the Vampire Slayer", 10.0) == 0
        assert _pack_score(t, "Buffy the Vampire Slayer", 1_000_000_000) == 0


def test_search_torrent_pack_cascades_past_dead_first_query(monkeypatch):
    from kometa.prowlarr_client import search_torrent_pack

    class FakeProwlarr:
        def search(self, q, protocol=None):
            if "complete" in q:
                # First query returns only a wrong-year/season release
                return [{"title": "Buffy the Vampire Slayer Season Eight (2007) Complete",
                         "size": 1e9, "seeders": 5, "categories": [], "magnet": "magnet:?xt=s8"}]
            # Second query returns the real 1998 pack
            return [{"title": "Buffy the Vampire Slayer (001-063) (1998) (Digital)",
                     "size": 2e9, "seeders": 10, "categories": [], "magnet": "magnet:?xt=1998"}]

    fake = FakeProwlarr()
    winner = search_torrent_pack(fake, "Buffy the Vampire Slayer", series_year=1998)
    assert winner is not None
    assert winner["magnet"] == "magnet:?xt=1998"


def test_a_single_issue_is_not_a_pack():
    from kometa.usenet_client import _pack_score
    b = "Buffy the Vampire Slayer"
    assert _pack_score(f"{b} 012 (1998) (digital)", b, 40_000_000) == 0
    assert _pack_score(f"{b} #12 (1998)", b, 40_000_000) == 0
    assert _pack_score(f"{b} 001 - 063 (1998-2003)", b, 2_000_000_000) > 10   # scene span, no '#'
    assert _pack_score(f"{b} (1998-2003) (Digital)", b, 2_000_000_000) > 10   # the run by its dates


def test_pack_cascade_does_not_settle_for_a_single(monkeypatch):
    from kometa.prowlarr_client import search_torrent_pack

    class FakeProwlarr:
        def search(self, q, protocol=None):
            if "complete" in q:
                return []
            return [{"title": "Buffy the Vampire Slayer 012 (1998) (digital)",
                     "size": 40_000_000, "seeders": 9, "categories": [], "magnet": "magnet:?xt=12"}]

    assert search_torrent_pack(FakeProwlarr(), "Buffy the Vampire Slayer", series_year=1998) is None
