"""The LOCG top-up queue: what to ask LOCG the next time a pass is live.

LOCG is a filler, never a dependency (decided 2026-10-10). Anything a filler
would have asked LOCG while the door was shut lands here instead — issue
details, cover lists, trade lists, credits for LOCG-only runs — and is drained
in a bounded burst the moment a pass works (the paste) and on a slow timer
while one stays live. The first refusal stops a drain; five failed tries drop
an item. Nothing in the app waits on this queue.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
KINDS = ("issue_details", "variants", "trades", "credits", "community")
MAX_ATTEMPTS = 5
DRAIN_BUDGET = 40
DRAIN_GAP_S = (1.5, 3.0)
_draining = {"on": False}


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS locg_topup (
            kind TEXT NOT NULL, target TEXT NOT NULL, queued_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY (kind, target))""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def enqueue(kind: str, target: str, path=None) -> None:
    if kind not in KINDS:
        raise ValueError(kind)
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO locg_topup (kind, target, queued_at) VALUES (?, ?, ?)", (kind, str(target), _now()))


def waiting(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM locg_topup GROUP BY kind")}
    return {"total": sum(by.values()), "by_kind": by}


def _series_of(kind: str, target: str) -> int | None:
    try:
        return int(target.split(":")[0])
    except (ValueError, AttributeError):
        return None


def _queue(path) -> list[dict]:
    """Oldest first, pull-list series first."""
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM locg_topup ORDER BY queued_at")]
        pulled = {r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE on_pull_list = 1")}
    rows.sort(key=lambda r: (_series_of(r["kind"], r["target"]) not in pulled, r["queued_at"]))
    return rows


def _run_one(item: dict, path) -> bool:
    """Call the right filler with LOCG allowed. True = done (row goes)."""
    from kometa import record
    kind, target = item["kind"], item["target"]
    sid = _series_of(kind, target)
    if sid is None:
        return True
    if kind in ("issue_details", "variants"):
        number = float(target.split(":")[1])
        if kind == "issue_details":
            return record.fill_issue(sid, number, path, force=True) is not None
        return record.fill_variants(sid, number, path, force=True) is not None
    if kind == "trades":
        record.fill_trades(sid, force=True, path=path)
        return True
    if kind == "credits":
        # the lowest owned issue LOCG knows → its details → the series' signals
        issues = [i for i in db.get_issues_for_series(sid, path) if i.get("locg_issue_id")]
        if not issues:
            return True
        rep = min([i for i in issues if i.get("owned")] or issues, key=lambda i: i["number"])
        if record.fill_issue(sid, rep["number"], path, force=True, issue=rep) is None:
            return False
        from kometa import related
        related.fill_signals(sid, path)
        return True
    # 'community': no fetcher yet — stays queued for the build that adds one
    return False


def drain(budget: int = DRAIN_BUDGET, path=None, is_open=None, run=None, sleep=time.sleep) -> dict:
    """One bounded burst: up to `budget` LOCG-backed fills, oldest first, a
    gap between, stopping at the first refusal. Returns the tally."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import locg_client
    is_open = is_open or (lambda: locg_client.locg_paused() is None)
    run = run or _run_one
    out = {"done": 0, "failed": 0, "dropped": 0, "stopped": None, "left": 0}
    if not is_open():
        out["stopped"] = "paused"
        out["left"] = waiting(path)["total"]
        return out
    calls = 0
    for item in _queue(path):
        if calls >= budget:
            out["stopped"] = "budget"
            break
        if not is_open():
            out["stopped"] = "paused"
            break
        if item["kind"] == "community":
            continue
        calls += 1
        try:
            ok = run(item, path)
            err = None
        except locg_client.LocgPaused as e:
            out["stopped"] = "paused"
            err = str(e)
            ok = False
            _bump(item, err, path)
            break
        except Exception as e:
            ok, err = False, str(e)[:200]
        with db._connect(path) as conn:
            if ok:
                conn.execute("DELETE FROM locg_topup WHERE kind = ? AND target = ?", (item["kind"], item["target"]))
                out["done"] += 1
            else:
                out["failed"] += 1
                if item["attempts"] + 1 >= MAX_ATTEMPTS:
                    conn.execute("DELETE FROM locg_topup WHERE kind = ? AND target = ?", (item["kind"], item["target"]))
                    out["dropped"] += 1
                else:
                    conn.execute("UPDATE locg_topup SET attempts = attempts + 1, last_error = ? WHERE kind = ? AND target = ?",
                                 (err, item["kind"], item["target"]))
        sleep(DRAIN_GAP_S[0] + (DRAIN_GAP_S[1] - DRAIN_GAP_S[0]) * (calls % 3) / 2)
    out["left"] = waiting(path)["total"]
    return out


def _bump(item: dict, err: str, path):
    with db._connect(path) as conn:
        conn.execute("UPDATE locg_topup SET attempts = attempts + 1, last_error = ? WHERE kind = ? AND target = ?",
                     (err[:200], item["kind"], item["target"]))


def drain_in_background(budget: int = DRAIN_BUDGET) -> bool:
    """For the paste moment and the button: never block a request on LOCG."""
    if _draining["on"]:
        return False
    _draining["on"] = True

    def go():
        try:
            r = drain(budget)
            logger.info(f"LOCG top-up: {r}")
        except Exception as e:
            logger.info(f"LOCG top-up failed: {e}")
        finally:
            _draining["on"] = False
    threading.Thread(target=go, daemon=True).start()
    return True


def scheduled_drain() -> int:
    """Scheduler tick: a small burst while a pass is live; nothing otherwise."""
    from kometa import locg_client
    if locg_client.locg_paused() is not None or not locg_client._access()[0]:
        return 0
    return drain(budget=20)["done"]


def status(path=None) -> dict:
    """For Settings: is a pass live, when does a pause lift, what's waiting."""
    from kometa import locg_client
    path = path or DB_PATH
    paused = locg_client.locg_paused()
    w = waiting(path)
    return {"locg_pass_live": bool(locg_client._access()[0]) and paused is None,
            "locg_paused_until": locg_client.pause_label(paused) if paused else None,
            "locg_topup_waiting": w["total"], "locg_topup_by_kind": w["by_kind"], "locg_topup_draining": _draining["on"]}
