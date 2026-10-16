"""Related, phase 2 (kometa/related_combined.py): themes read out of descriptions,
"liked by the same readers", and the anthology guard — each on a small shelf."""
import json

import pytest

import kometa.cbr as cbr
import kometa.cbr_people as people
import kometa.db as db
import kometa.record as rec
import kometa.related as rel
import kometa.related_combined as rc


@pytest.fixture(autouse=True)
def _fresh_index():
    rc._cache.update(ix=None, path=None, fp=None, at=0.0, opts=None)
    rc._with_cache.clear()
    rc._theme_memo.clear()
    rel._nb_cache.update(value=None, path=None, at=0.0)
    yield


def _run(db_path, title, credits_by_issue, descs=None, publisher="Image Comics"):
    sid = db.add_series(title=title, publisher=publisher, folder_path=None, on_pull_list=False, path=db_path)
    for n, credits in enumerate(credits_by_issue, 1):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 1, path=db_path)
        desc = (descs or [""] * len(credits_by_issue))[n - 1]
        rec.write_issue(sid, float(n), {"desc": desc, "credits": [{"role": r, "name": nm} for r, nm in credits]},
                        "metron", "full", {"metron_issue_id": sid * 1000 + n}, db_path)
    return sid


def _row(db_path, sid, **opts):
    return {c["series_id"]: c for c in rc.related_combined(sid, path=db_path, opts={"themes": False, "community": "off", **opts})}


def test_a_one_issue_anthology_is_nobodys_creator_link_but_a_two_writer_run_still_is(db_path):
    staples = ("artist", "Fiona Staples")
    saga = _run(db_path, "Saga", [[("writer", "Brian K. Vaughan"), staples]] * 3)
    joker80 = _run(db_path, "The Joker 80th Anniversary", [[staples] + [("writer", f"Writer {i}") for i in range(12)]])
    duo = _run(db_path, "Staples Duo", [[staples, ("writer", "Partner")]])
    # the catalogue's creator list must not bring the anthology's contributors back
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, '[]')",
                  (joker80, json.dumps([{"name": "Fiona Staples", "role": "artist"}])))
    row = _row(db_path, saga)
    assert joker80 not in row
    assert row[duo]["why"][0] == "Artist: Fiona Staples"
    assert _row(db_path, joker80)[saga]["why"][0] == "Artist: Fiona Staples"     # its own row still walks them
    # eight writers an issue over two issues is a busy run, not an anthology: still a hub to damp, not to drop
    busy = _run(db_path, "Busy", [[("writer", f"W{i}") for i in range(8)]] * 2)
    one_offs: dict = {}
    rc._owned_credits(db_path, one_offs)
    assert busy not in one_offs and one_offs[joker80] >= {"fiona staples"}


def test_themes_from_descriptions_link_two_runs_only_with_the_switch_on(db_path):
    a = _run(db_path, "Night Feeders", [[("writer", "A")]] * 3,
             descs=["A vampire hunts the city.", "The vampires gather.", "Dracula wakes."])
    b = _run(db_path, "Blood Coven", [[("writer", "B")]] * 3,
             descs=["Vampires rule the night.", "A vampire queen.", "Nosferatu rises."])
    _run(db_path, "Something Else", [[("writer", "C")]] * 3, descs=["A heist.", "Another heist.", "The getaway driver."])
    assert b not in _row(db_path, a)
    card = _row(db_path, a, themes=True)[b]
    assert card["why"][0] == "Theme: vampires" and "genre" in card["channels"]


def test_a_name_is_not_a_theme(db_path):
    sid = _run(db_path, "Watchmen", [[("writer", "Alan Moore")]] * 3,
               descs=["The Silk Spectre visits Mars.", "Laurie, the Silk Spectre, remembers.", "Spectre and Owl."])
    witchy = _run(db_path, "Coven", [[("writer", "D")]] * 2, descs=["The witches meet.", "A witch is burned."])
    themes = rc.description_themes(db_path)
    assert "ghosts" not in themes.get(sid, [])
    assert themes[witchy] == ["occult"]                     # the vocabulary's name, so a file's "occult" matches


def _readers(db_path, sids_scores):
    """{person: {sid: score}} as CBR people scores on linked runs."""
    cbr.ensure_tables(db_path)
    people.ensure_tables(db_path)
    with db._connect(db_path) as c:
        for sid in {s for scores in sids_scores.values() for s in scores}:
            c.execute("INSERT OR REPLACE INTO cbr_series (tracked_series_id, state, url) VALUES (?, 'linked', ?)",
                      (sid, f"https://comicbookroundup.com/comic-books/reviews/image-comics/run-{sid}/"))
        for person, scores in sids_scores.items():
            for sid, score in scores.items():
                c.execute("INSERT INTO cbr_people_scores (person, series_path, number, score, at) VALUES (?, ?, '1', ?, '2026-10-16')",
                          (person, f"/comic-books/reviews/image-comics/run-{sid}", score))


def test_liked_by_the_same_readers_boosts_a_card_and_makes_one_only_when_allowed(db_path):
    seed = _run(db_path, "Monstress", [[("writer", "Marjorie Liu")]] * 2)
    genre_mate = _run(db_path, "Seven to Eternity", [[("writer", "Rick Remender")]] * 2)
    stranger = _run(db_path, "Era Hit", [[("writer", "Somebody")]] * 2)
    flop = _run(db_path, "Flop", [[("writer", "Nobody")]] * 2)
    for sid in (seed, genre_mate):
        rec.set_tags(sid, "metron", ["Fantasy"], db_path)
    for i in range(12):
        rec.set_tags(_run(db_path, f"Filler {i}", [[("writer", f"F{i}")]] * 2), "metron", ["Crime"], db_path)
    # 40 readers love Monstress, Seven to Eternity and Era Hit and pan Flop
    _readers(db_path, {f"user:{i}": {seed: 9.5, genre_mate: 9.0, stranger: 9.0, flop: 3.0} for i in range(40)})

    plain = _row(db_path, seed)
    assert stranger not in plain and "community" not in plain[genre_mate]["channels"]

    boost = _row(db_path, seed, community="boost")
    assert stranger not in boost and flop not in boost                       # a boost never makes a card
    assert boost[genre_mate]["score"] > plain[genre_mate]["score"]
    assert rc.COMMUNITY_REASON in boost[genre_mate]["why"]

    cards = _row(db_path, seed, community="cards")
    assert cards[stranger]["why"] == [rc.COMMUNITY_REASON] and flop not in cards


def test_a_handful_of_shared_readers_is_not_a_link(db_path):
    seed = _run(db_path, "Seed", [[("writer", "A")]] * 2)
    other = _run(db_path, "Other", [[("writer", "B")]] * 2)
    pad = _run(db_path, "Pad", [[("writer", "C")]] * 2)
    _readers(db_path, {f"user:{i}": {seed: 9.0, other: 9.0, pad: 4.0} for i in range(4)})   # four, under the five needed
    assert other not in _row(db_path, seed, community="cards")


def test_the_switches_come_from_config_and_a_change_rebuilds_the_index(db_path, monkeypatch):
    monkeypatch.setattr(rc, "DB_PATH", db_path)
    _run(db_path, "Solo", [[("writer", "A")]] * 2)
    assert rc.options(db_path) == {"themes": False, "community": "off"}
    first = rc.index(db_path)
    db.set_config({"related_themes": "on", "related_community": "boost"}, db_path)
    assert rc.options(db_path) == {"themes": True, "community": "boost"}
    second = rc.index(db_path)
    assert second is not first and second.opts == {"themes": True, "community": "boost"}
    db.set_config({"related_community": "nonsense"}, db_path)
    assert rc.options(db_path)["community"] == "off"
