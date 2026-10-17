"""The credits ladder (record.fill_credits / _credits_to_keep; docs/credits-sourcing-design-2026-10-17.md,
reviewed in credits-sourcing-review-2026-10-17.md): one catalogue's list, the best that has any —
Metron > GCD > Comic Vine > LOCG > file tags; an empty answer never clears a list."""
import json

import kometa.db as db
import kometa.record as record
import kometa.related as rel

AZZ = [{"role": "Writer", "name": "Brian Azzarello", "metron_creator_id": None},
       {"role": "Penciller", "name": "Eduardo Risso", "metron_creator_id": None}]


def _row(db_path, credits, source="metron", extra=None, n=4.0):
    sid = db.add_series(title=f"100 Bullets {n}", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, n, "2026-10-14", 1, path=db_path)
    record.write_issue(sid, n, {"desc": "x", "credits": credits, "extra": extra if extra is not None else {"gcd_id": 2872386}},
                       source, "full", {"metron_issue_id": 181754}, db_path)
    return sid


def test_illustrator_is_an_artist():
    assert rel.role_groups(["Illustrator"]) == ["artist"] and rel._role_weight("Illustrator") == 2.0


def test_an_empty_answer_never_clears_a_list_and_a_better_rung_replaces_it(db_path):
    sid = _row(db_path, [])
    out = record.fill_credits(sid, 4.0, db_path, rungs=(("gcd", lambda r: AZZ), ("comicvine", lambda r: 1 / 0)))
    assert out["source"] == "gcd"
    row = record.get_issue(sid, 4.0, db_path)
    assert row["credits"] == AZZ and record.credits_source(row) == "gcd"
    # Metron asked again, still none: GCD's list stays, and says where it came from
    record.write_issue(sid, 4.0, {"desc": "x", "credits": [], "extra": {"gcd_id": 2872386}}, "metron", "full", {}, db_path)
    row = record.get_issue(sid, 4.0, db_path)
    assert row["credits"] == AZZ and record.credits_source(row) == "gcd"
    # Metron catches up: its list wins
    mine = [{"role": "Writer", "name": "Brian Azzarello", "metron_creator_id": 1}]
    record.write_issue(sid, 4.0, {"desc": "x", "credits": mine}, "metron", "full", {}, db_path)
    row = record.get_issue(sid, 4.0, db_path)
    assert row["credits"] == mine and record.credits_source(row) == "metron"
    # a lower rung never replaces a higher one
    record.write_issue(sid, 4.0, {"desc": "x", "credits": AZZ}, "locg", "partial", {}, db_path)
    assert record.get_issue(sid, 4.0, db_path)["credits"] == mine


def test_the_ladder_falls_through_and_remembers_what_it_asked(db_path):
    sid = _row(db_path, [], extra={"gcd_id": 1, "cv_id": 2})
    asked = []
    rungs = (("gcd", lambda r: asked.append("gcd") or []), ("comicvine", lambda r: asked.append("cv") or AZZ))
    assert record.fill_credits(sid, 4.0, db_path, rungs=rungs)["source"] == "comicvine"
    row = record.get_issue(sid, 4.0, db_path)
    assert set(row["field_sources"]["credits_tried"]) == {"gcd", "comicvine"}
    # a rung that had nothing waits a week; one that can't be asked (no id, no budget) isn't recorded
    sid2 = _row(db_path, [], extra={"gcd_id": 1}, n=5.0)
    record.fill_credits(sid2, 5.0, db_path, rungs=(("gcd", lambda r: []), ("comicvine", lambda r: None)))
    asked.clear()
    record.fill_credits(sid2, 5.0, db_path, rungs=(("gcd", lambda r: asked.append("gcd") or AZZ), ("comicvine", lambda r: None)))
    assert asked == []
    assert set(record.get_issue(sid2, 5.0, db_path)["field_sources"]["credits_tried"]) == {"gcd"}


def test_a_dry_run_writes_nothing(db_path):
    sid = _row(db_path, [])
    out = record.fill_credits(sid, 4.0, db_path, rungs=(("gcd", lambda r: AZZ),), dry_run=True)
    assert out["source"] == "gcd" and record.get_issue(sid, 4.0, db_path)["credits"] == []
    assert record.get_issue(sid, 4.0, db_path)["field_sources"] == {}


def test_metron_credits_are_never_sent_down_the_ladder(db_path):
    sid = _row(db_path, AZZ)
    out = record.fill_credits(sid, 4.0, db_path, rungs=(("gcd", lambda r: 1 / 0),))
    assert out == {"source": None, "credits": [], "asked": []}


def test_comic_vine_roles_become_metron_roles():
    got = record._cv_credits([{"name": "Sana Takeda", "role": "artist, colorist, cover"},
                              {"name": "Jackie Marzan", "role": "letterer, production"},
                              {"name": "Someone", "role": "editor"}, {"name": "Marjorie Liu", "role": "writer"}])
    assert [(c["name"], c["role"]) for c in got] == [("Sana Takeda", "Artist"), ("Sana Takeda", "Colorist"),
                                                       ("Sana Takeda", "Cover"), ("Jackie Marzan", "Letterer"),
                                                       ("Marjorie Liu", "Writer")]


def test_gcd_credits_leave_out_editors(db_path, monkeypatch):
    import kometa.gcd_client as gcd
    monkeypatch.setattr(gcd, "configured", lambda: True)
    monkeypatch.setattr(gcd, "budget", lambda: {"left": 100})
    monkeypatch.setattr(gcd, "issue_detail", lambda gid: {"credits": [{"role": "Writer", "name": "Christopher Cantwell"},
                                                                       {"role": "Editor", "name": "Ben Abernathy"},
                                                                       {"role": "Cover", "name": "Jacob Phillips"}]})
    got = record._ask_gcd({"extra": {"gcd_id": 2872320}})
    assert [(c["role"], c["name"]) for c in got] == [("Writer", "Christopher Cantwell"), ("Cover", "Jacob Phillips")]


def test_the_switch_is_off_until_the_owner_turns_it_on(db_path, monkeypatch):
    _row(db_path, [])
    called = []
    monkeypatch.setattr(record, "fill_credits", lambda *a, **k: called.append(a) or {"source": None})
    assert record.credits_tick(db_path) == 0 and called == []
    db.set_config({"credits_ladder": "on"}, path=db_path)
    record.credits_tick(db_path)
    assert len(called) == 1
