"""The Related evaluation harness (kometa/related_eval.py): its arithmetic, on a
small shelf with a fake engine whose answers are known."""
import json
import os
import sqlite3

import kometa.db as db
import kometa.related as rel
import kometa.related_eval as ev


def _shelf(db_path, n=10):
    ids = [db.add_series(title=f"Run {i}", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
           for i in range(n)]
    rel.ensure_tables(db_path)
    return ids


def test_the_gates_count_coverage_hubs_crowding_and_weak_credits(db_path):
    ids = _shelf(db_path)
    hub = ids[1]
    bkv = {"role": "writer", "name": "Brian K. Vaughan", "id": 7}
    joe = {"role": "inker", "name": "Joe Rubinstein", "id": 3}
    with db._connect(db_path) as c:                    # seed 0 shares a writer with runs 2–5, only an inker with run 9
        for sid, creators in [(ids[0], [bkv, joe])] + [(x, [bkv]) for x in ids[2:6]] + [(ids[9], [joe])]:
            c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json, fetched_at) VALUES (?, ?, '[]', datetime('now'))",
                      (sid, json.dumps(creators)))

    def engine(sid, path):
        if sid == ids[0]:                              # 4 cards, all for one writer, plus the inker-only card
            return [{"series_id": x, "title": f"Run {x}", "why": ["Brian K. Vaughan"], "score": 3.0} for x in ids[2:6]] + \
                   [{"series_id": ids[9], "title": "Run 9", "why": ["Joe Rubinstein"], "score": 1.0}]
        return [{"series_id": hub, "title": "Run 1", "why": ["arc: Knightfall"], "score": 3.0}] if sid != hub else []

    r = ev.evaluate(db_path, engine, seeds=["Run 0", "Not Here"])
    assert r["series"] == 10 and r["covered"] == 1 and r["empty"] == 1
    assert r["hub_top"][0] == ("Run 1", 8) and abs(r["hub_max_share"] - 0.8) < 1e-9
    assert r["crowded"] == [("Run 0", "Brian K. Vaughan", 4)]
    assert r["weak_cards"] == [("Run 0", "Run 9")]
    assert r["seeds"][1]["series_id"] is None and r["seeds"][0]["cards"][4]["weak"] is True
    passed = {g: ok for g, _, ok in ev.gates(r)}
    assert not any(passed.values()) or passed["Speed, 95th percentile"]          # only speed can pass here


def test_reason_kinds_and_precision_from_the_owners_marks(db_path):
    assert ev.reason_kind("arc: Knightfall") == "arc" and ev.reason_kind("Franchise: Aliens") == "franchise"
    assert ev.reason_kind("on a reading list together") == "list" and ev.reason_kind("Mike Mignola") == "creator"
    assert ev.reason_kind("Theme: post-apocalyptic") == "genre" and ev.reason_kind("Team: X-Men") == "cast"
    seeds = [{"title": "Saga", "series_id": 31, "cards": [{"series_id": s, "title": t} for s, t in
                                                           ((622, "Paper Girls"), (385, "Ex Machina"), (9, "Batman"))]}]
    p = ev._precision(seeds, {"31": {"622": "good", "385": "ok", "9": "bad"}})
    assert p == {"precision": 2 / 3, "marked": 3, "unmarked": 0}
    assert ev._precision(seeds, {"Saga": {"Paper Girls": "good"}})["unmarked"] == 2   # by title works too
    assert ev._precision(seeds, {}) is None


def test_it_runs_on_a_copy_and_never_writes_the_source(db_path, tmp_path):
    _shelf(db_path, 3)
    before = os.path.getmtime(db_path), sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM tracked_series").fetchone()
    copy = ev.snapshot(db_path)
    try:
        assert copy != db_path and sqlite3.connect(copy).execute("SELECT COUNT(*) FROM tracked_series").fetchone() == before[1]
        r = ev.evaluate(copy, ev.baseline_engine)
        assert r["series"] == 3 and "## Engine: baseline" in ev.report({"baseline": r})
    finally:
        ev.discard(copy)
    assert not [f for f in os.listdir(os.path.dirname(copy)) if f.startswith(os.path.basename(copy))]   # no journal left behind
    assert os.path.getmtime(db_path) == before[0]
