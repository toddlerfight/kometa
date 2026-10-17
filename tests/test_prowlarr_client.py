

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
