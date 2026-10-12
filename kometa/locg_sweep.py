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
BUDGET = 30           # one page per issue now (variants + details + community from one fetch)
REQUEST_GAP_S = 3.0
_today = {"day": None, "done": 0}


def _sync_in_progress() -> bool:
    from kometa import sync
    with sync._sync_locks_guard:
        return any(l.locked() for l in sync._sync_locks.values())


# What the sweep owes an owned issue with a LOCG id (2026-10-12, enrichment 1e):
#   - LOCG-only issues: the variant list, and the details when thin
#   - issues Metron knows: LOCG's long tail of variants on top of Metron's — but only
#     once Metron has filled the record, since the merge reuses Metron's covers from
#     the record (this sweep must never be the thing that makes a Metron call)
#   - every one: the community snapshot (rating, pulls, have/read/want)
_OWED = """
    i.owned = 1 AND i.locg_issue_id IS NOT NULL AND i.locg_issue_id != ''
    AND (s.kind IS NULL OR s.kind != 'arc')
    AND (
      (i.metron_issue_id IS NULL AND (r.variants_at IS NULL OR r.fill_state = 'variants'))
      OR (i.metron_issue_id IS NOT NULL AND r.source = 'metron' AND r.variants_at IS NULL)
      OR (r.tracked_series_id IS NOT NULL AND r.community_at IS NULL
          AND (i.metron_issue_id IS NULL OR r.source = 'metron'))
      OR (r.tracked_series_id IS NULL AND i.metron_issue_id IS NULL)
    )"""


def candidates(path=None, limit: int = BUDGET) -> list[dict]:
    """Owned issues the sweep owes something (see _OWED): pull list first, newest first."""
    path = path or DB_PATH
    from kometa.record import ensure_tables
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute(f"""
            SELECT i.*, s.on_pull_list FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE {_OWED}
            ORDER BY s.on_pull_list DESC, i.store_date DESC, i.tracked_series_id, i.number
            LIMIT ?""", (limit,))]


def pending(path=None) -> int:
    path = path or DB_PATH
    from kometa.record import ensure_tables
    ensure_tables(path)
    with db._connect(path) as conn:
        return conn.execute(f"""
            SELECT COUNT(*) FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE {_OWED}""").fetchone()[0]


def done_today() -> int:
    return _today["done"] if _today["day"] == date.today() else 0


def _count(n: int = 1) -> None:
    if _today["day"] != date.today():
        _today.update(day=date.today(), done=0)
    _today["done"] += n


def _from_record(path):
    """Metron's half of a merge, from the record — never a Metron call."""
    from kometa import record
    return lambda issue, p=None: {"covers": (record.get_issue(issue["tracked_series_id"], issue["number"], path) or {}).get("covers") or []}


def sweep_tick(budget: int = BUDGET, path=None, is_open=None, fill_variants=None, fill_issue=None,
               sleep=time.sleep, syncing=None, community=None, page_cached=None) -> dict:
    """One tick. Returns {requests, variants, details, community, stopped}."""
    path = path or DB_PATH
    from kometa import locg_client, record
    is_open = is_open or (lambda: locg_client.locg_paused() is None)
    syncing = syncing or _sync_in_progress
    out = {"requests": 0, "variants": 0, "details": 0, "community": 0, "stopped": None}
    if not is_open():
        out["stopped"] = "paused"; return out
    if syncing():
        out["stopped"] = "sync in progress"; return out
    fv = fill_variants or (lambda sid, n, issue: record.fill_variants(
        sid, n, path, issue=issue, metron=_from_record(path) if issue.get("metron_issue_id") else None))
    fc = community or locg_client.fetch_community
    cached = page_cached or (lambda lid: str(lid) in locg_client._page_cache)
    fi = fill_issue or (lambda sid, n, issue: record.fill_issue(sid, n, path, issue=issue))
    from kometa.activity import yield_to_reader
    for issue in candidates(path, limit=budget):
        yield_to_reader(sleep)
        sid, n = issue["tracked_series_id"], issue["number"]
        if out["requests"] >= budget:
            break
        if out["requests"]:
            sleep(REQUEST_GAP_S + random.uniform(0, 0.5))
        known = record.get_issue(sid, n, path)
        if known and known.get("variants_at"):
            got = known.get("covers") or []                 # covers already here: details only this time
        else:
            out["requests"] += 1
            try:
                got = fv(sid, n, issue)
            except Exception as e:
                out["stopped"] = f"refused: {e}"; break
            if not is_open() or got is None:        # the client paused itself, or nothing could be asked
                out["stopped"] = "refused"; break
            out["variants"] += 1
        # details too, when LOCG is the only catalogue for this issue and the record is thin
        if not issue.get("metron_issue_id") and out["requests"] < budget:
            row = record.get_issue(sid, n, path)
            # the variant fetch just above may have made a covers-only row: that is
            # not the details, so ask (it used to look done and the ask was skipped)
            if not row or row.get("fill_state") in ("variants", None) or (not row.get("desc") and not row.get("credits")):
                if not cached(issue["locg_issue_id"]):           # the variants just fetched this page: free
                    sleep(REQUEST_GAP_S + random.uniform(0, 0.5))
                    out["requests"] += 1
                try:
                    r = fi(sid, n, issue)
                except Exception as e:
                    out["stopped"] = f"refused: {e}"; break
                if not is_open():
                    out["stopped"] = "refused"; break
                if r is None:
                    # LOCG answered but had nothing: say so on the row, or this
                    # issue heads the queue and stalls the sweep every tick
                    record.write_issue(sid, n, {}, "locg", "partial", issue, path)
                out["details"] += 1
        # the community snapshot — from the same page, so usually free
        row = record.get_issue(sid, n, path)
        if not (row or {}).get("community_at"):
            if not cached(issue["locg_issue_id"]):
                if out["requests"] >= budget:
                    break
                sleep(REQUEST_GAP_S + random.uniform(0, 0.5))
                out["requests"] += 1
            try:
                record.set_community(sid, n, fc(issue["locg_issue_id"]), path)
            except Exception as e:
                out["stopped"] = f"refused: {e}"; break
            if not is_open():
                out["stopped"] = "refused"; break
            out["community"] += 1
    _count(out["requests"])
    logger.info(f"LOCG sweep: {out['requests']} requests, {out['variants']} variant lists, {out['details']} details, "
                f"{out['community']} community"
                + (f", stopped: {out['stopped']}" if out["stopped"] else ""))
    return out


def scheduled_tick() -> dict:
    try:
        return sweep_tick()
    except Exception as e:
        logger.info(f"LOCG sweep skipped: {e}")
        return {"requests": 0, "variants": 0, "details": 0, "stopped": str(e)}
