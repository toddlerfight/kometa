"""The LOCG top-up queue (kometa/topup.py)."""
import kometa.db as db
import kometa.topup as tp
import kometa.locg_client as lc


def test_enqueue_is_idempotent_and_counts_by_kind(db_path):
    tp.enqueue("trades", "5", db_path); tp.enqueue("trades", "5", db_path); tp.enqueue("variants", "5:1", db_path)
    assert tp.waiting(db_path) == {"total": 2, "by_kind": {"trades": 1, "variants": 1}}


def test_drain_runs_oldest_pull_list_first_stops_on_refusal_and_drops_after_five(db_path):
    pulled = db.add_series(title="A", publisher="P", folder_path=None, on_pull_list=True, path=db_path)
    idle = db.add_series(title="B", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    tp.enqueue("trades", str(idle), db_path)
    tp.enqueue("issue_details", f"{pulled}:3", db_path)
    tp.enqueue("community", str(pulled), db_path)
    ran = []
    def run(item, path):
        ran.append((item["kind"], item["target"]))
        if item["kind"] == "trades":
            raise RuntimeError("boom")
        return True
    r = tp.drain(budget=10, path=db_path, is_open=lambda: True, run=run, sleep=lambda s: None)
    assert ran == [("issue_details", f"{pulled}:3"), ("trades", str(idle))]          # pull list first; community skipped
    assert (r["done"], r["failed"], r["dropped"], r["left"]) == (1, 1, 0, 2)
    for _ in range(4):
        tp.drain(budget=10, path=db_path, is_open=lambda: True, run=run, sleep=lambda s: None)
    assert tp.waiting(db_path)["by_kind"] == {"community": 1}                          # five failures: dropped
    # a refusal mid-drain stops everything
    tp.enqueue("variants", f"{pulled}:1", db_path); tp.enqueue("variants", f"{pulled}:2", db_path)
    calls = []
    def refuse(item, path):
        calls.append(item["target"]); raise lc.LocgPaused("LOCG is pausing us")
    r = tp.drain(budget=10, path=db_path, is_open=lambda: True, run=refuse, sleep=lambda s: None)
    assert r["stopped"] == "paused" and len(calls) == 1
    assert tp.drain(budget=10, path=db_path, is_open=lambda: False, run=refuse, sleep=lambda s: None)["stopped"] == "paused"


def test_fillers_enqueue_instead_of_asking_while_shut(db_path):
    import kometa.record as rec
    sid = db.add_series(title="Hellboy", publisher="DH", folder_path=None, on_pull_list=False, path=db_path)
    db.set_locg_link(sid, "linked", db_path)
    db.upsert_issue_status(sid, 1.0, "2020-01-01", 0, locg_issue_id="41", path=db_path)
    assert rec.fill_issue(sid, 1.0, db_path, locg_open=lambda: False) is None
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET locg_series_id = 123 WHERE id = ?", (sid,))
    rec.fill_trades(sid, path=db_path, locg_open=lambda: False, enrich=lambda s, t, books=None: None)
    assert tp.waiting(db_path)["by_kind"] == {"issue_details": 1, "trades": 1}
