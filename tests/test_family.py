"""Families (kometa/family.py): a run's specials fold under it for display."""
from kometa.family import attach_families


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
            _s(9, "- Dark Nights - Metal - The Casting", "One-Shot", owned=1)]
    attach_families(rows)
    by = {r["id"]: r for r in rows}
    assert by[2]["family_parent"] == 1 and by[3]["family_parent"] == 1
    assert sorted(by[1]["family_children"]) == [2, 3]
    assert by[4]["family_parent"] is None
    assert by[6]["family_parent"] is None and by[7]["family_parent"] == 5
    assert by[9]["family_parent"] == 8                                   # leading '- ' ignored, ' - ' subtitle counts
