"""The record trickle's LOCG issue-page asks rest after three failures in a row, sharing the
sweep's rest (635 failed asks in a day, 25 s each, 2026-10-18). While resting, an issue Metron
lacks is queued for later instead of asked."""
import kometa.db as db
import kometa.record as record
from kometa import locg_sweep, topup


def test_three_page_failures_rest_the_issue_pages_for_everyone(db_path, monkeypatch):
    monkeypatch.setattr(locg_sweep, "_rest", {"until": 0.0, "strikes": 0})
    monkeypatch.setattr(locg_sweep, "_page_fails", {"n": 0})
    queued = []
    monkeypatch.setattr(topup, "enqueue", lambda kind, key, path=None: queued.append(key))
    sid = db.add_series(title="Absolute Batman", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    for n in range(1, 6):
        db.upsert_issue_status(sid, float(n), "2026-11-04", 0, locg_issue_id=str(1000 + n), path=db_path)
    asked = []

    def walled(issue, path):
        asked.append(issue["number"])
        raise RuntimeError("HTTP 500 — the challenge")
    from kometa import locg_client
    monkeypatch.setattr(locg_client, "locg_paused", lambda: False)
    for n in range(1, 6):
        record.fill_issue(sid, float(n), db_path, locg=walled)
    assert asked == [1.0, 2.0, 3.0]                       # three failures, then the pages rest
    assert locg_sweep.pages_resting() and queued == [f"{sid}:4", f"{sid}:5"]
    assert locg_sweep.scheduled_tick(tick=lambda: 1 / 0)["stopped"].startswith("resting")   # the sweep rests too
