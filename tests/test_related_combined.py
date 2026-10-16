"""Related, phase 1 (kometa/related_combined.py): the rules from the design rounds,
each on a small shelf built for it."""
import json

import pytest

import kometa.db as db
import kometa.record as rec
import kometa.related as rel
import kometa.related_combined as rc


@pytest.fixture(autouse=True)
def _fresh_index():
    rc._cache.update(ix=None, path=None, fp=None, at=0.0)
    rel._nb_cache.update(value=None, path=None, at=0.0)
    yield


def _run(db_path, title, credits_by_issue, owned=None, publisher="Image Comics", metron=None, mtype=None):
    """A series with one record per issue; `owned` = the issue numbers on disk (default all)."""
    sid = db.add_series(title=title, publisher=publisher, folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_series_id = ?, metron_type = ? WHERE id = ?", (metron, mtype, sid))
    for n, credits in enumerate(credits_by_issue, 1):
        is_owned = owned is None or n in owned
        db.upsert_issue_status(sid, float(n), "2020-01-01", 1 if is_owned else 0, path=db_path)
        rec.write_issue(sid, float(n), {"credits": [{"role": r, "name": nm} for r, nm in credits]}, "metron", "full",
                        {"metron_issue_id": sid * 1000 + n}, db_path)
    return sid


def test_a_writer_cannot_fill_the_top_six_a_weak_credit_makes_no_card_and_genre_reaches_monstress(db_path):
    bkv, wooton, inker = ("writer", "Brian K. Vaughan"), ("letterer", "Fonografiks"), ("inker", "Joe Rubinstein")
    saga = _run(db_path, "Saga", [[bkv, ("artist", "Fiona Staples"), wooton, inker]] * 3)
    bkv_runs = [_run(db_path, f"BKV Run {i}", [[bkv]] * 2) for i in range(6)]
    monstress = _run(db_path, "Monstress", [[("writer", "Marjorie Liu"), wooton]] * 2)
    inked = _run(db_path, "Inked Elsewhere", [[("writer", "Somebody Else"), inker]] * 2)
    others = [_run(db_path, f"Space Opera {i}", [[("writer", f"Writer {i}")]] * 2) for i in range(3)]
    rec.set_tags(saga, "metron", ["Fantasy", "Science Fiction"], db_path)
    rec.set_tags(monstress, "metron", ["Fantasy"], db_path)
    rec.set_tags(monstress, "comicinfo", ["leading ladies"], db_path)
    for o in others:
        rec.set_tags(o, "metron", ["Science Fiction"], db_path)
    row = rc.related_combined(saga, path=db_path)
    ids = [c["series_id"] for c in row]
    assert inked not in ids                                                 # a shared inker alone: no card
    top6 = row[:6]
    assert sum(c["why"][0] == "Writer: Brian K. Vaughan" for c in top6) == 3
    assert monstress in ids and set(bkv_runs) <= set(ids)                   # held-back writer cards still fill the row
    m = next(c for c in row if c["series_id"] == monstress)
    assert m["why"][0] == "Genre: fantasy" and "creators" in m["channels"]  # the letterer adds, the genre carries
    assert all(c["channels"] for c in row)                                   # every card carries its channels
    assert [c["score"] for c in row[:6] if c["why"][0] != "Writer: Brian K. Vaughan"] == sorted(
        [c["score"] for c in row[:6] if c["why"][0] != "Writer: Brian K. Vaughan"], reverse=True)


def test_creators_count_over_the_issues_you_own(db_path):
    """Batman (1940): over 693 credited issues the 20% rule kept four 1940s names. Over
    the issues you own, it keeps the people you read."""
    old, moench = ("writer", "Bill Finger"), ("writer", "Doug Moench")
    long_run = _run(db_path, "Batman (1940)", [[old]] * 10 + [[moench]] * 2, owned={11, 12}, publisher="DC Comics")
    knightfall = _run(db_path, "Knightfall", [[moench]] * 2, publisher="DC Comics")
    finger = _run(db_path, "Golden Age Thing", [[old]] * 2, publisher="DC Comics")
    row = {c["series_id"]: c for c in rc.related_combined(long_run, path=db_path)}
    assert knightfall in row and row[knightfall]["why"][0] == "Writer: Doug Moench"
    assert finger not in row


def test_a_runs_own_annual_is_never_its_recommendation_but_its_franchise_and_companions_are(db_path):
    ab = _run(db_path, "Absolute Batman", [[("writer", "Scott Snyder")]] * 13, publisher="DC Comics", metron=500)
    annual = _run(db_path, "Absolute Batman Annual", [[("writer", "Scott Snyder")]], publisher="DC Comics",
                  metron=501, mtype="Annual")
    companion = _run(db_path, "Absolute Wonder Woman", [[("writer", "Kelly Thompson")]] * 2, publisher="DC Comics", metron=502)
    rec.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_record (tracked_series_id, extra_json, source) VALUES (?, ?, 'metron')",
                  (ab, json.dumps({"associated": [{"id": 501, "name": "Absolute Batman Annual"}, {"id": 502, "name": "AWW"}]})))
    a1 = _run(db_path, "Aliens - Berserker", [[("letterer", "Steve Dutro")]], publisher="Dark Horse Comics")
    a2 = _run(db_path, "Aliens - Alchemy", [[("writer", "John Arcudi")]], publisher="Dark Horse Comics")
    row = {c["series_id"]: c for c in rc.related_combined(ab, path=db_path)}
    assert annual not in row                                                 # family, even though Metron links it
    assert row[companion]["why"][0] == "Companion series"
    aliens = {c["series_id"]: c for c in rc.related_combined(a1, path=db_path)}
    assert aliens[a2]["why"] == ["Franchise: Aliens"]


def test_the_series_page_uses_the_combiner_only_when_switched_on(db_path, monkeypatch):
    monkeypatch.setattr(rel, "DB_PATH", db_path)
    monkeypatch.setattr(rc, "DB_PATH", db_path)
    used = []
    monkeypatch.setattr(rel, "related", lambda sid, *a, **k: used.append("old") or [])
    monkeypatch.setattr(rc, "related_combined", lambda sid, **k: used.append("new") or [])
    rel._row(1)
    db.set_config({"related_engine": "combined"}, db_path)
    rel._row(1)
    monkeypatch.setattr(rc, "related_combined", lambda sid, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rel._row(1)                                                              # a scoring bug falls back, never blanks the page
    assert used == ["old", "new", "old"]


def _with_universe(db_path, sid, universe, issues=3):
    with db._connect(db_path) as c:
        for n in range(1, issues + 1):
            c.execute("UPDATE issue_record SET extra_json = ? WHERE tracked_series_id = ? AND number = ?",
                      (json.dumps({"universes": [universe]}), sid, float(n)))


def test_a_universe_links_its_runs_however_many_share_it_but_a_default_continuity_never_does(db_path):
    absolute = [_run(db_path, f"Absolute Run {i}", [[("writer", f"Writer A{i}")]] * 3, publisher="DC Comics") for i in range(5)]
    mainline = [_run(db_path, f"Mainline {i}", [[("writer", f"Writer M{i}")]] * 3, publisher="Marvel Comics") for i in range(3)]
    for sid in absolute:
        _with_universe(db_path, sid, "Absolute Universe")
    for sid in mainline:
        _with_universe(db_path, sid, "Earth 616")
    row = {c["series_id"]: c for c in rc.related_combined(absolute[0], path=db_path)}
    assert set(absolute[1:]) <= set(row) and row[absolute[1]]["why"][0] == "Absolute Universe"
    assert rc.related_combined(mainline[0], path=db_path) == []


def test_a_series_in_many_rows_is_scaled_down(db_path):
    hub = _run(db_path, "Anthology", [[("writer", f"Writer {i}") for i in range(8)]] * 2)
    runs = [_run(db_path, f"Run {i}", [[("writer", f"Writer {i}"), ("writer", f"Partner {i % 4}")]] * 2) for i in range(8)]
    ix = rc.index(db_path)
    assert ix.damp.get(hub, 1.0) < 1.0 and all(ix.damp.get(r, 1.0) == 1.0 for r in runs)


def test_review_fixes_one_card_per_family_team_names_the_cast_and_a_strong_genre_says_so(db_path):
    bkv = ("writer", "Brian K. Vaughan")
    saga = _run(db_path, "Saga", [[bkv]] * 3)
    em = _run(db_path, "Ex Machina", [[bkv]] * 3)
    special = _run(db_path, "Ex Machina Special", [[bkv]], mtype="One-Shot")
    pg = _run(db_path, "Paper Girls", [[bkv]] * 3)
    for sid in (saga, pg):
        rec.set_tags(sid, "metron", ["Science Fiction"], db_path)
    row = rc.related_combined(saga, path=db_path)
    titles = [c["title"] for c in row]
    assert "Ex Machina" in titles and "Ex Machina Special" not in titles     # one card per family
    assert next(c for c in row if c["title"] == "Paper Girls")["why"] == ["Writer: Brian K. Vaughan", "Genre: science fiction"]

    ff = _run(db_path, "Fantastic Four", [[("writer", "Writer A")]] * 3, publisher="Marvel Comics")
    ff2 = _run(db_path, "FF", [[("writer", "Writer B")]] * 3, publisher="Marvel Comics")
    for sid in (ff, ff2):
        with db._connect(db_path) as c:
            for n in (1.0, 2.0, 3.0):
                c.execute("UPDATE issue_record SET extra_json = ? WHERE tracked_series_id = ? AND number = ?",
                          (json.dumps({"teams": ["Fantastic Four"], "characters": ["Human Torch", "Thing"]}), sid, n))
    rc._cache.update(ix=None)
    card = next(c for c in rc.related_combined(ff, path=db_path) if c["series_id"] == ff2)
    assert card["why"][0] == "Team: Fantastic Four"


def test_a_long_run_you_own_none_of_keeps_its_two_year_creators(db_path):
    """Batman (1940): 693 catalogue issues, none owned. 20% meant 139 issues; Doug
    Moench's 103 fell out. Over 50 issues with nothing owned, 25 is enough."""
    moench, finger = ("writer", "Doug Moench"), ("writer", "Bill Finger")
    long_run = _run(db_path, "Batman (1940)", [[finger]] * 150 + [[moench]] * 30, owned=set(), publisher="DC Comics")
    knightfall = _run(db_path, "Knightfall", [[moench]] * 2, publisher="DC Comics")
    row = {c["series_id"]: c for c in rc.related_combined(long_run, path=db_path)}
    assert row[knightfall]["why"][0] == "Writer: Doug Moench"
