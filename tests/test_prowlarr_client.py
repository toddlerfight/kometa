

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
