"""The LOCG sweep: variants (and details) for owned issues while a pass is live.

Variants used to fill only when a modal's tab opened or the Metron trickle
reached an issue — five lists in nine thousand issues. This asks LOCG for the
owned issues that carry a LOCG id and have no variant list yet, pull list and
newest first, in a cadence that cannot get the house banned again:

  - one request every REQUEST_GAP_S seconds, never two at once
  - at most BUDGET requests a tick (variant fetches AND detail fetches count)
  - one tick every ten minutes (the scheduler), and never while a sync holds
    a series lock — a sync is already talking to LOCG
  - the tick stops at the FIRST refusal and lets the client's pause stand;
    nothing is retried inside a tick
"""
import logging
import random
import time
from datetime import date

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
BUDGET = 20
REQUEST_GAP_S = 3.0
_today = {"day": None, "done": 0}


def _sync_in_progress() -> bool:
    from kometa import sync
    with sync._sync_locks_guard:
        return any(l.locked() for l in sync._sync_locks.values())


def candidates(path=None, limit: int = BUDGET) -> list[dict]:
    """Owned issues LOCG alone can answer for — a LOCG id and NO Metron id — with
    no variant list yet: pull list first, newest store date first. An issue
    Metron knows is the Metron trickle's (it merges LOCG when open): this sweep
    must never be the thing that makes a Metron call."""
    path = path or DB_PATH
    from kometa.record import ensure_tables
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.*, s.on_pull_list FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND i.locg_issue_id IS NOT NULL AND i.locg_issue_id != ''
              AND i.metron_issue_id IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc') AND r.variants_at IS NULL
            ORDER BY s.on_pull_list DESC, i.store_date DESC, i.tracked_series_id, i.number
            LIMIT ?""", (limit,))]


def pending(path=None) -> int:
    path = path or DB_PATH
    from kometa.record import ensure_tables
    ensure_tables(path)
    with db._connect(path) as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND i.locg_issue_id IS NOT NULL AND i.locg_issue_id != ''
              AND i.metron_issue_id IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc') AND r.variants_at IS NULL""").fetchone()[0]


def done_today() -> int:
    return _today["done"] if _today["day"] == date.today() else 0


def _count(n: int = 1) -> None:
    if _today["day"] != date.today():
        _today.update(day=date.today(), done=0)
    _today["done"] += n


def sweep_tick(budget: int = BUDGET, path=None, is_open=None, fill_variants=None, fill_issue=None,
               sleep=time.sleep, syncing=None) -> dict:
    """One tick. Returns {requests, variants, details, stopped}."""
    path = path or DB_PATH
    from kometa import locg_client, record
    is_open = is_open or (lambda: locg_client.locg_paused() is None)
    syncing = syncing or _sync_in_progress
    out = {"requests": 0, "variants": 0, "details": 0, "stopped": None}
    if not is_open():
        out["stopped"] = "paused"; return out
    if syncing():
        out["stopped"] = "sync in progress"; return out
    fv = fill_variants or (lambda sid, n, issue: record.fill_variants(sid, n, path, issue=issue))
    fi = fill_issue or (lambda sid, n, issue: record.fill_issue(sid, n, path, issue=issue))
    from kometa.activity import yield_to_reader
    for issue in candidates(path, limit=budget):
        yield_to_reader(sleep)
        sid, n = issue["tracked_series_id"], issue["number"]
        if out["requests"] >= budget:
            break
        if out["requests"]:
            sleep(REQUEST_GAP_S + random.uniform(0, 0.5))
        out["requests"] += 1
        try:
            got = fv(sid, n, issue)
        except Exception as e:
            out["stopped"] = f"refused: {e}"; break
        if not is_open() or got is None:            # the client paused itself, or nothing could be asked
            out["stopped"] = "refused"; break
        out["variants"] += 1
        # details too, when LOCG is the only catalogue for this issue and the record is thin
        if not issue.get("metron_issue_id") and out["requests"] < budget:
            row = record.get_issue(sid, n, path)
            if not row or row.get("fill_state") != "full":
                sleep(REQUEST_GAP_S + random.uniform(0, 0.5))
                out["requests"] += 1
                try:
                    r = fi(sid, n, issue)
                except Exception as e:
                    out["stopped"] = f"refused: {e}"; break
                if not is_open() or r is None:
                    out["stopped"] = "refused"; break
                out["details"] += 1
    _count(out["requests"])
    logger.info(f"LOCG sweep: {out['requests']} requests, {out['variants']} variant lists, {out['details']} details"
                + (f", stopped: {out['stopped']}" if out["stopped"] else ""))
    return out


def scheduled_tick() -> dict:
    try:
        return sweep_tick()
    except Exception as e:
        logger.info(f"LOCG sweep skipped: {e}")
        return {"requests": 0, "variants": 0, "details": 0, "stopped": str(e)}
