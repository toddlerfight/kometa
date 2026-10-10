"""The LOCG variant sweep (kometa/locg_sweep.py): owned + LOCG id + unfilled first, polite, stops at a refusal."""
import kometa.db as db
import kometa.locg_sweep as sw
import kometa.record as rec


def _series(db_path, title, pull=False):
    return db.add_series(title=title, publisher="X", folder_path=None, on_pull_list=pull, path=db_path)


def test_tick_picks_owned_locg_unfilled_pull_list_first_and_queues_images(db_path):
    a = _series(db_path, "Quiet")
    b = _series(db_path, "Pulled", pull=True)
    db.upsert_issue_status(a, 1.0, "2026-01-01", 1, locg_issue_id="11", path=db_path)
    db.upsert_issue_status(a, 2.0, "2026-02-01", 0, locg_issue_id="12", path=db_path)     # not owned: skipped
    db.upsert_issue_status(a, 3.0, "2026-03-01", 1, path=db_path)                         # no locg id: skipped
    db.upsert_issue_status(a, 4.0, "2026-04-01", 1, locg_issue_id="14", metron_issue_id=99, path=db_path)   # Metron's job, never the sweep's
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


def test_budget_counts_details_and_variants_together(db_path):
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
    assert out["requests"] == 10 and reqs["v"] + reqs["d"] == 10



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
