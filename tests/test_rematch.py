"""Re-match (kometa/rematch.py): unlinked series asked again, inside the app, on the one throttle."""
import kometa.db as db
import kometa.rematch as rm
from kometa.metron_client import MetronUnavailable


def _unlinked(db_path, title, pull=False):
    sid = db.add_series(title=title, publisher="X", folder_path=None, on_pull_list=pull, path=db_path)
    db.set_metron_link(sid, "none", db_path)
    return sid


def test_pull_list_first_links_confident_hits_and_remembers_misses(db_path, monkeypatch):
    import kometa.metron_client as mc
    monkeypatch.setattr(mc, "configured", lambda: True)
    a = _unlinked(db_path, "Zed")
    b = _unlinked(db_path, "Alpha", pull=True)
    c = _unlinked(db_path, "Mid")
    linked_a = db.add_series(title="Linked", publisher="X", folder_path=None, on_pull_list=False, path=db_path)
    db.set_metron_series_id(linked_a, 5, db_path)
    asked, synced, sig = [], [], []
    find = lambda s: (asked.append(s["title"]) or (77 if s["title"] == "Alpha" else None))
    out = rm.rematch_tick(limit=8, path=db_path, find=find, sync=lambda s: synced.append(s["id"]), signals=lambda i: sig.append(i))
    assert asked == ["Alpha", "Mid", "Zed"]                               # pull list first, then A–Z; linked series not asked
    assert {k: out[k] for k in ("asked", "linked", "stopped")} == {"asked": 3, "linked": 1, "stopped": None}
    assert db.get_series_by_id(b, db_path)["metron_series_id"] == 77 and synced[0] == b and sig == [b]   # later syncs are the empty-series pass
    assert rm.candidates(db_path) == []                                    # misses remembered: nobody re-asked today
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET metron_checked_at = datetime('now', '-31 days') WHERE id = ?", (a,))
    assert [s["title"] for s in rm.candidates(db_path)] == ["Zed"]        # a month on, asked again


def test_stops_at_metrons_first_refusal(db_path, monkeypatch):
    import kometa.metron_client as mc
    monkeypatch.setattr(mc, "configured", lambda: True)
    _unlinked(db_path, "A"); _unlinked(db_path, "B")
    calls = []
    def find(s):
        calls.append(s["title"]); raise MetronUnavailable("Metron asked us to wait")
    out = rm.rematch_tick(path=db_path, find=find)
    assert calls == ["A"] and out["stopped"] and out["asked"] == 0
    assert len(rm.candidates(db_path)) == 2                                # nothing stamped: both asked next tick


def test_a_linked_series_with_no_issues_is_resynced(db_path, monkeypatch):
    import kometa.rematch as rm
    import kometa.metron_client as mc
    monkeypatch.setattr(mc, "configured", lambda: True)
    empty = db.add_series(title="The Amazing Spider-Man", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    db.set_metron_series_id(empty, 10958, db_path); db.set_metron_link(empty, "linked", db_path)
    full = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.set_metron_series_id(full, 916, db_path); db.set_metron_link(full, "linked", db_path)
    db.upsert_issue_status(full, 1.0, "2012-03-14", 1, path=db_path)
    synced = []
    out = rm.rematch_tick(path=db_path, find=lambda s: None, sync=lambda s: synced.append(s["title"]), signals=lambda sid: None)
    assert synced == ["The Amazing Spider-Man"] and out.get("resynced") == 1


def test_recheck_asks_metron_then_locg_and_hands_the_rest_to_a_person(db_path):
    import kometa.rematch as rm
    from kometa.shelf_import import NEEDS_MATCH
    ids = {}
    for t in ("Batman- Hush", "The Golden Child", "Aliens Technical Manual", "Chosen None"):
        sid = db.add_series(title=t, publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
        db.set_metron_link(sid, "none", db_path); db.set_match_status(sid, "manual", db_path)
        ids[t] = sid
    rm.ensure_columns(db_path)
    with db._connect(db_path) as c:            # a person's 'no run' is never queued
        c.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (rm.NEVER, ids["Chosen None"]))
    assert rm.recheck_seed(db_path) == 3
    assert rm.recheck_seed(db_path) == 3      # seeded once
    import kometa.metron_client as mc
    real = mc.configured; mc.configured = lambda: True
    try:
        locg_asked, slept = [], []
        out = rm.recheck_tick(path=db_path, find_metron=lambda s: 501 if "Hush" in s["title"] else None,
                              find_locg=lambda s: locg_asked.append(s["title"]) or (144336 if "Golden" in s["title"] else None),
                              locg_open=lambda: True, sync=lambda s: None, signals=lambda sid: None, sleep=slept.append)
    finally:
        mc.configured = real
    assert out["metron"] == 1 and out["locg"] == 1 and out["needs_match"] == 1 and out["left"] == 0
    assert db.get_series_by_id(ids["Batman- Hush"], db_path)["metron_series_id"] == 501
    assert db.get_series_by_id(ids["The Golden Child"], db_path)["locg_series_id"] == 144336
    assert db.get_series_by_id(ids["Aliens Technical Manual"], db_path)["match_status"] == NEEDS_MATCH
    assert "Batman- Hush" not in locg_asked                       # LOCG only for Metron's misses
    assert slept and all(s >= rm.LOCG_GAP_S for s in slept)


def test_recheck_stops_at_metron_refusal_and_keeps_the_queue(db_path):
    import kometa.rematch as rm
    import kometa.metron_client as mc
    sid = db.add_series(title="Deadpool- Samurai", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    db.set_metron_link(sid, "none", db_path); db.set_match_status(sid, "manual", db_path)
    rm.recheck_seed(db_path)
    real = mc.configured; mc.configured = lambda: True
    try:
        def refuse(s): raise mc.MetronUnavailable("banned")
        out = rm.recheck_tick(path=db_path, find_metron=refuse, find_locg=lambda s: 1 / 0, locg_open=lambda: True,
                              sync=lambda s: None, signals=lambda sid: None, sleep=lambda s: None)
    finally:
        mc.configured = real
    assert out["stopped"] and out["left"] == 1                     # Metron first, always: LOCG never jumped the queue
