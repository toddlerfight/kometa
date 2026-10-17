"""The credits ladder after its implementation review (docs/credits-ladder-impl-review-2026-10-17.md):
a row the ladder can't improve isn't picked again every tick (finding 1), and a list naming
neither a writer nor an artist loses to one that does (decision 1, Hit-Monkey #1)."""
import kometa.db as db
import kometa.record as record

TEAM = [{"role": "Writer", "name": "Daniel Way", "metron_creator_id": None},
        {"role": "Artist", "name": "Dalibor Talajić", "metron_creator_id": None}]
COVER = [{"role": "Cover", "name": "Humberto Ramos", "metron_creator_id": None}]
LOCG = [{"role": "Writer", "name": "Daniel Way", "metron_creator_id": None}]


def _row(db_path, credits, source="metron", extra=None, n=1.0):
    sid = db.add_series(title=f"Hit-Monkey {n}", publisher="Marvel", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, n, "2010-04-07", 1, path=db_path)
    record.write_issue(sid, n, {"desc": "x", "credits": credits, "extra": extra if extra is not None else {"gcd_id": 1, "cv_id": 2}},
                       source, "full", {"metron_issue_id": 9}, db_path)
    return sid


def _wanted(db_path):
    return {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}


def test_a_row_gcd_credited_in_full_is_not_picked_again(db_path):
    sid = _row(db_path, LOCG, source="locg")
    assert (sid, 1.0) in _wanted(db_path)
    out = record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: TEAM), ("comicvine", lambda r: 1 / 0)))
    assert out["source"] == "gcd" and out["asked"] == ["gcd"]          # Comic Vine never asked
    assert (sid, 1.0) not in _wanted(db_path)                           # and the row isn't picked next tick


def test_a_cover_only_gcd_list_falls_through_to_comic_vines_team(db_path):
    sid = _row(db_path, [])
    out = record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: COVER), ("comicvine", lambda r: TEAM)))
    assert out["source"] == "comicvine" and out["asked"] == ["gcd", "comicvine"]
    row = record.get_issue(sid, 1.0, db_path)
    assert row["credits"] == TEAM and record.credits_source(row) == "comicvine"      # one source's list, whole


def test_with_no_team_anywhere_gcds_cover_list_is_kept(db_path):
    sid = _row(db_path, [])
    out = record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: COVER), ("comicvine", lambda r: [])))
    assert out["source"] == "gcd" and record.get_issue(sid, 1.0, db_path)["credits"] == COVER


def test_a_cover_only_gcd_list_is_asked_about_again_when_comic_vine_can_be(db_path):
    sid = _row(db_path, [], extra={"gcd_id": 1})                       # no Comic Vine id yet
    record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: COVER), ("comicvine", lambda r: None)))
    assert (sid, 1.0) not in _wanted(db_path)                           # GCD asked this week, nothing else to ask
    with db._connect(db_path) as conn:                                  # Metron learns the Comic Vine id
        conn.execute("UPDATE issue_record SET cv_issue_id = '2' WHERE tracked_series_id = ?", (sid,))
    assert (sid, 1.0) in _wanted(db_path)
    out = record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: 1 / 0), ("comicvine", lambda r: TEAM)))
    assert out["source"] == "comicvine"


def test_a_list_without_the_team_never_replaces_one_with_it(db_path):
    sid = _row(db_path, [])
    record.fill_credits(sid, 1.0, db_path, rungs=(("gcd", lambda r: []), ("comicvine", lambda r: TEAM)))
    # Metron comes back with only the cover artist: Comic Vine's team stays
    record.write_issue(sid, 1.0, {"desc": "x", "credits": COVER}, "metron", "full", {}, db_path)
    row = record.get_issue(sid, 1.0, db_path)
    assert row["credits"] == TEAM and record.credits_source(row) == "comicvine"
    # Metron with nothing: still Comic Vine's
    record.write_issue(sid, 1.0, {"desc": "x", "credits": []}, "metron", "full", {}, db_path)
    assert record.get_issue(sid, 1.0, db_path)["credits"] == TEAM
    # Metron with the team: Metron's
    mine = [dict(TEAM[0], metron_creator_id=7)]
    record.write_issue(sid, 1.0, {"desc": "x", "credits": mine}, "metron", "full", {}, db_path)
    row = record.get_issue(sid, 1.0, db_path)
    assert row["credits"] == mine and record.credits_source(row) == "metron"


def test_gcd_isnt_asked_with_no_budget_left(monkeypatch):
    from kometa import gcd_client
    monkeypatch.setattr(gcd_client, "configured", lambda: True)
    monkeypatch.setattr(gcd_client, "budget", lambda: {"left": 0})
    monkeypatch.setattr(gcd_client, "issue_detail", lambda gid: 1 / 0)
    assert record._ask_gcd({"extra": {"gcd_id": 5}}) is None             # couldn't ask: not recorded as tried
