"""Families (kometa/family.py): a run's specials fold under it for display."""
from kometa.family import attach_families, attach_franchises


def _s(i, title, mtype=None, owned=0, missing=0):
    return {"id": i, "title": title, "metron_type": mtype, "owned": owned, "missing": missing, "upcoming": 0}


def test_specials_fold_under_the_run_and_ongoings_do_not():
    rows = [_s(1, "Absolute Batman", "Single Issue", owned=24),
            _s(2, "Absolute Batman: Ark M", "One-Shot", owned=1),
            _s(3, "Absolute Batman 2025 Annual", "Annual", owned=1),
            _s(4, "Absolute Batman Rising", None, owned=3, missing=9),      # unknown type, ≤12 issues, but no separator/special word
            _s(5, "Batman", "Single Issue", owned=100),
            _s(6, "Batman: The Dark Knight Returns", "Limited Series", owned=4),   # one-word parent: a subtitle is NOT a special
            _s(7, "Batman Annual 2025", "Annual", owned=1),
            _s(8, "Dark Nights - Metal", "Limited Series", owned=6),
            _s(9, "- Dark Nights - Metal - The Casting", "One-Shot", owned=1),
            dict(_s(10, "Batman Beyond (2012)", "Single Issue", owned=30), year_began=2012),
            dict(_s(11, "Batman Beyond - Volume 01 (1999)", "Limited Series", owned=6), year_began=1999),
            dict(_s(12, "Batman Beyond - Rebirth", "One-Shot", owned=1), year_began=2016)]
    attach_families(rows)
    by = {r["id"]: r for r in rows}
    assert by[2]["family_parent"] == 1 and by[3]["family_parent"] == 1
    assert sorted(by[1]["family_children"]) == [2, 3]
    assert by[4]["family_parent"] is None
    assert by[6]["family_parent"] is None and by[7]["family_parent"] == 5
    assert by[9]["family_parent"] == 8                                   # leading '- ' ignored, ' - ' subtitle counts
    assert by[11]["family_parent"] is None and by[12]["family_parent"] == 10   # an older run is not a special of a newer one


def test_franchises_stack_five_or_more_leading_names():
    """Aliens stacks, Star Wars keys on two words, a four-member group doesn't
    stack, year prefixes and suffixes are ignored, a special stays under its run
    inside the stack."""
    i = iter(range(1, 200))
    rows = []
    aliens = ["Aliens", "[1992] Aliens - Platinum Edition", "Aliens - Alchemy", "Aliens - Berserker",
              "Aliens - Book One", "Aliens vs. Predator (1990)", "Aliens - Harvest"]
    rows += [_s(next(i), t, "Limited Series", owned=4) for t in aliens]
    rows += [_s(next(i), t, "Limited Series", owned=3) for t in
             ("Star Wars - Visions (2022)", "Star Wars: Legacy of Vader", "Star Wars - Rogue One", "Star Wars - The Mandalorian",
              "Star Wars - Darth Vader", "Star Trek - Picard")]
    rows += [_s(next(i), t, "Limited Series", owned=3) for t in ("Rumble (2014)", "Rumble (2017)", "Rumble - Special", "Rumble Deluxe")]
    bat = _s(next(i), "Batman (2025)", "Single Issue", owned=24); ark = _s(next(i), "Batman Annual 2025", "Annual", owned=1)
    rows += [bat, ark] + [_s(next(i), t, "Single Issue", owned=10) for t in ("Batman - Rebirth (2016)", "Batman Beyond (2012)", "Batman- Three Jokers (2020)", "Batman: Hush (2003)")]
    attach_franchises(attach_families(rows))
    by = {r["title"]: r for r in rows}
    assert by["Aliens - Alchemy"]["franchise"] == {"key": "aliens", "name": "Aliens", "count": 7}
    assert by["[1992] Aliens - Platinum Edition"]["franchise"]["key"] == "aliens"
    assert by["Aliens vs. Predator (1990)"]["franchise"]["key"] == "aliens"
    assert by["Star Wars - Visions (2022)"]["franchise"]["name"] == "Star Wars" and by["Star Wars - Visions (2022)"]["franchise"]["count"] == 5
    assert by["Star Trek - Picard"]["franchise"] is None                         # 'star trek' alone is one
    assert by["Rumble (2014)"]["franchise"] is None                              # four members: no stack
    assert by["Batman (2025)"]["franchise"]["name"] == "Batman" and by["Batman (2025)"]["franchise"]["count"] == 5
    assert by["Batman Annual 2025"]["family_parent"] == bat["id"] and by["Batman Annual 2025"]["franchise"]["key"] == "batman"   # a special rides inside


def test_zombies_stacks_on_zombies_vs_and_names_from_the_shared_words():
    """'Zombies vs Robots' is the run; 'Zombies Christmas Carol' is not part of it."""
    from kometa.family import attach_franchises
    titles = ["Zombies Christmas Carol", "Zombies Vs. Robots - Undercity", "Zombies Vs. Robots Aventure",
              "Zombies vs Robots", "Zombies vs Robots Classic", "Zombies vs Robots Complete", "Zombies vs Robots vs Amazons"]
    rows = attach_franchises([_s(i + 1, t) for i, t in enumerate(titles)])
    by = {r["title"]: r["franchise"] for r in rows}
    assert by["Zombies Christmas Carol"] is None
    assert {f["name"] for t, f in by.items() if f} == {"Zombies vs Robots"}
