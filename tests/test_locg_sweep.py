"""The LOCG variant sweep (kometa/locg_sweep.py): owned + LOCG id + unfilled first, polite, stops at a refusal."""
import kometa.db as db
import kometa.locg_sweep as sw
import kometa.record as rec
import pytest


@pytest.fixture(autouse=True)
def _community(monkeypatch):
    """No network: the community snapshot comes from a stub, and every page counts
    as a fresh fetch unless a test says otherwise."""
    from kometa import locg_client
    calls = []
    monkeypatch.setattr(locg_client, "fetch_community", lambda lid: calls.append(lid) or {"rating": 4.0})
    monkeypatch.setattr(locg_client, "_page_cache", {})
    return calls


def _series(db_path, title, pull=False):
    return db.add_series(title=title, publisher="X", folder_path=None, on_pull_list=pull, path=db_path)


def test_tick_picks_owned_locg_unfilled_pull_list_first_and_queues_images(db_path):
    a = _series(db_path, "Quiet")
    b = _series(db_path, "Pulled", pull=True)
    db.upsert_issue_status(a, 1.0, "2026-01-01", 1, locg_issue_id="11", path=db_path)
    db.upsert_issue_status(a, 2.0, "2026-02-01", 0, locg_issue_id="12", path=db_path)     # not owned: skipped
    db.upsert_issue_status(a, 3.0, "2026-03-01", 1, path=db_path)                         # no locg id: skipped
    db.upsert_issue_status(a, 4.0, "2026-04-01", 1, locg_issue_id="14", metron_issue_id=99, path=db_path)   # Metron hasn't filled it yet: not the sweep's turn
    db.upsert_issue_status(b, 1.0, "2025-01-01", 1, locg_issue_id="21", path=db_path)
    asked, slept = [], []
    def fv(sid, n, issue):
        asked.append((sid, n))
        return rec.fill_variants(sid, n, db_path, issue=issue, locg_open=lambda: True,
                                 locg=lambda lid: [{"id": "v1", "name": "Cover B 1:25 Variant", "thumb": "https://s3/x.jpg", "large": "https://s3/x.jpg"}])
    def details(sid, n, issue):           # what the real LOCG details fill does: write the row
        return rec.write_issue(sid, n, {"desc": "d", "credits": [{"role": "Writer", "name": "A"}]}, "locg", "partial", issue, db_path)
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=fv, fill_issue=details, sleep=slept.append, syncing=lambda: False)
    assert asked == [(b, 1.0), (a, 1.0)]                                                  # pull list first
    assert out["variants"] == 2 and out["stopped"] is None
    assert all(s >= sw.REQUEST_GAP_S for s in slept) and len(slept) >= 1                   # never faster than the gap
    row = rec.get_issue(b, 1.0, db_path)
    assert row["variants_at"] and row["credits"]                                          # details landed...
    assert any(c.get("id") == "v1" for c in row["covers"])                                 # ...and the variants survived them
    from kometa import images
    keys = [k for k, _, _ in images.pending(db_path)]
    assert images.variant_key(b, 1.0, "v1") in keys                                        # the images trickle will fetch it
    assert sw.pending(db_path) == 0


def test_tick_stops_at_the_first_refusal_and_does_nothing_while_paused_or_syncing(db_path):
    a = _series(db_path, "A", pull=True)
    for n in (1.0, 2.0, 3.0):
        db.upsert_issue_status(a, n, "2026-01-01", 1, locg_issue_id=str(10 + int(n)), path=db_path)
    calls = []
    open_state = {"open": True}
    def fv(sid, n, issue):
        calls.append(n); open_state["open"] = False; return None                            # LOCG refused: client paused itself
    out = sw.sweep_tick(path=db_path, is_open=lambda: open_state["open"], fill_variants=fv, sleep=lambda s: None, syncing=lambda: False)
    assert calls == [1.0] and out["stopped"] == "refused" and out["requests"] == 1
    assert sw.sweep_tick(path=db_path, is_open=lambda: False, sleep=lambda s: None)["stopped"] == "paused"
    assert sw.sweep_tick(path=db_path, is_open=lambda: True, syncing=lambda: True, sleep=lambda s: None)["stopped"] == "sync in progress"


def test_budget_counts_details_variants_and_community_together(db_path, _community):
    a = _series(db_path, "A", pull=True)
    for n in range(1, 30):
        db.upsert_issue_status(a, float(n), "2026-01-01", 1, locg_issue_id=str(100 + n), path=db_path)   # no metron id → details too
    reqs = {"v": 0, "d": 0}
    def fv(sid, n, issue):
        reqs["v"] += 1
        return rec.fill_variants(sid, n, db_path, issue=issue, locg_open=lambda: True, locg=lambda lid: [])
    def fi(sid, n, issue):
        reqs["d"] += 1; return {"ok": True}
    out = sw.sweep_tick(budget=10, path=db_path, is_open=lambda: True, fill_variants=fv, fill_issue=fi, sleep=lambda s: None, syncing=lambda: False)
    assert out["requests"] == 10 and reqs["v"] + reqs["d"] + len(_community) == 10



def test_a_covers_only_row_still_gets_its_details(db_path):
    """2026-10-10: the variant fetch made a covers-only row, the details step took
    it for done details and never asked — Die #4-20 had covers and no credits."""
    a = _series(db_path, "Die")
    db.upsert_issue_status(a, 6.0, "2019-06-01", 1, locg_issue_id="7449295", path=db_path)
    rec.fill_variants(a, 6.0, db_path, issue={"tracked_series_id": a, "number": 6.0, "locg_issue_id": "7449295"},
                      locg_open=lambda: True, locg=lambda lid: [{"id": "v1", "name": "Cover B", "thumb": "t", "large": "l"}])
    assert rec.get_issue(a, 6.0, db_path)["fill_state"] == "variants"
    assert sw.pending(db_path) == 1
    asked = []
    def details(sid, n, issue):
        asked.append(n)
        return rec.write_issue(sid, n, {"desc": "d", "credits": [{"role": "Writer", "name": "Kieron Gillen"}]}, "locg", "partial", issue, db_path)
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=lambda *x: (_ for _ in ()).throw(AssertionError("variants again")),
                        fill_issue=details, sleep=lambda s: None, syncing=lambda: False)
    assert asked == [6.0] and out["details"] == 1 and out["variants"] == 0
    assert sw.pending(db_path) == 0


def test_metron_known_issue_gets_locg_variants_merged_from_the_record_never_a_metron_call(db_path, monkeypatch):
    """1e (2026-10-12): issues Metron knows get LOCG's long tail too, once Metron
    has filled the record — Metron's covers come from the record, not the API."""
    from kometa import issue_meta
    monkeypatch.setattr(issue_meta, "_metron", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Metron called")))
    a = _series(db_path, "Midnight Spider-Man", pull=True)
    issue = {"tracked_series_id": a, "number": 1.0, "metron_issue_id": 181715, "locg_issue_id": "3951157"}
    db.upsert_issue_status(a, 1.0, "2026-10-07", 1, locg_issue_id="3951157", metron_issue_id=181715, path=db_path)
    rec.write_issue(a, 1.0, {"desc": "m", "covers": [{"id": "m181715", "name": "Cover A (Main)", "thumb": "t", "large": "l"}]},
                    "metron", "full", issue, db_path)
    from kometa import locg_client
    monkeypatch.setattr(locg_client, "fetch_variants", lambda lid: {"covers": [
        {"id": "3951157", "name": "Cover A (Main)", "thumb": "t", "large": "l"},
        {"id": "v9", "name": "Big Time Collectibles NYCC 2026 ACO Variant", "thumb": "t9", "large": "l9"}]})
    monkeypatch.setattr(locg_client, "locg_paused", lambda: None)
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, sleep=lambda s: None, syncing=lambda: False)
    row = rec.get_issue(a, 1.0, db_path)
    names = [c["name"] for c in row["covers"]]
    assert names == ["Cover A (Main)", "Big Time Collectibles NYCC 2026 ACO Variant"]   # Metron's main kept, LOCG's tail added
    assert row["community"] == {"rating": 4.0} and row["community_at"]
    assert out["variants"] == 1 and out["community"] == 1 and sw.pending(db_path) == 0


def test_a_broken_page_is_skipped_for_a_day_and_the_tick_moves_on(db_path):
    """One page LOCG answered 500 sat first in line and stopped every tick for an evening.
    Now a non-pause failure skips that issue (for a day) and the sweep carries on; a real
    pause still stops the tick."""
    import requests
    from kometa import locg_client
    sw._bad.clear()
    a = _series(db_path, "A", pull=True)
    for n in (1.0, 2.0, 3.0):
        db.upsert_issue_status(a, n, "2026-01-01", 1, locg_issue_id=str(10 + int(n)), path=db_path)
    calls = []
    def fv(sid, n, issue):
        calls.append(n)
        if n == 1.0:                                                     # first in line, like the real stall
            raise requests.HTTPError("HTTP 500 for https://leagueofcomicgeeks.com/comic/11/comic")
        return []
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=fv, sleep=lambda s: None, syncing=lambda: False)
    assert calls == [1.0, 2.0, 3.0] and out["stopped"] is None and out["skipped"] == 1
    calls.clear()
    sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=fv, sleep=lambda s: None, syncing=lambda: False)
    assert calls == [2.0, 3.0]                                           # the bad page skipped without a request
    sw._bad.clear()
    def paused(sid, n, issue):
        calls.append(("p", n)); raise locg_client.LocgPaused("LOCG is pausing us")
    calls.clear()
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=paused, sleep=lambda s: None, syncing=lambda: False)
    assert calls == [("p", 1.0)] and out["stopped"].startswith("refused")


def test_three_failures_in_a_row_is_the_wall_and_the_pages_are_not_blamed(db_path):
    import requests
    sw._bad.clear()
    a = _series(db_path, "A", pull=True)
    for n in (1.0, 2.0, 3.0, 4.0):
        db.upsert_issue_status(a, n, "2026-01-01", 1, locg_issue_id=str(10 + int(n)), path=db_path)
    calls = []
    def fv(sid, n, issue):
        calls.append(n); raise requests.HTTPError("HTTP 500 (FlareSolverr timed out)")
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=fv, sleep=lambda s: None, syncing=lambda: False)
    assert calls == [1.0, 2.0, 3.0] and out["stopped"].startswith("refused: 3 pages in a row") and out["skipped"] == 0
    assert sw._bad == {}                                                  # nothing held back for a day
    # one failure then successes: that one page IS skipped, the tick carries on
    calls.clear()
    def fv2(sid, n, issue):
        calls.append(n)
        if n == 1.0:
            raise requests.HTTPError("HTTP 500")
        return []
    out = sw.sweep_tick(path=db_path, is_open=lambda: True, fill_variants=fv2, sleep=lambda s: None, syncing=lambda: False)
    assert out["stopped"] is None and out["skipped"] == 1 and set(sw._bad) == {"11"}
