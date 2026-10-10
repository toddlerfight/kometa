"""Re-match: series the first matcher left without a Metron run get asked again.

The early pass stamped 242 series 'none' before the title fixes landed, and a
'none' was never re-asked — so a run Metron plainly has (The Walking Dead
Deluxe) went without Related, trades and the record. This asks again, inside
the app, on the ONE Metron throttle: a detached script with its own pacing
doubled the request rate and earned a 24-hour ban (2026-10-10). Confident
matches only; a miss is remembered for RECHECK_DAYS so nobody is asked weekly.
"""
import logging
from datetime import datetime, timedelta, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
RECHECK_DAYS = 30
PER_TICK = 8


def ensure_columns(path=None):
    with db._connect(path or DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
        if "metron_checked_at" not in cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN metron_checked_at TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def candidates(path=None, limit: int = PER_TICK, now: datetime | None = None) -> list[dict]:
    """Unlinked series worth asking: no Metron id, marked 'none', not asked in
    RECHECK_DAYS. Pull list first, then alphabetical."""
    path = path or DB_PATH
    ensure_columns(path)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM tracked_series
            WHERE metron_series_id IS NULL AND metron_link = 'none' AND (kind IS NULL OR kind != 'arc')
              AND (metron_checked_at IS NULL OR metron_checked_at < ?)
            ORDER BY on_pull_list DESC, title COLLATE NOCASE""", (cutoff,))]
    return rows[:limit]


def rematch_tick(limit: int = PER_TICK, path=None, find=None, sync=None, signals=None) -> dict:
    """One scheduler tick. Stops at Metron's first refusal (the client has
    already paused everyone). Returns {asked, linked, stopped}."""
    path = path or DB_PATH
    from kometa import metron_client
    if not metron_client.configured():
        return {"asked": 0, "linked": 0, "stopped": "not configured"}
    if find is None:
        from kometa.shelf_import import find_metron_match as find
    out = {"asked": 0, "linked": 0, "stopped": None}
    for s in candidates(path, limit):
        try:
            mid = find(s)
        except metron_client.MetronUnavailable as e:
            out["stopped"] = str(e)
            logger.info(f"Re-match paused: {e}")
            break
        except Exception as e:
            logger.info(f"Re-match: {s['title']!r} skipped: {e}")
            continue
        out["asked"] += 1
        with db._connect(path) as conn:
            conn.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (_now(), s["id"]))
        if not mid:
            continue
        db.set_metron_series_id(s["id"], mid, path)
        db.set_metron_link(s["id"], "linked", path)
        out["linked"] += 1
        logger.info(f"Re-match: {s['title']!r} → Metron {mid}")
        fresh = db.get_series_by_id(s["id"], path)
        try:
            if sync is not None:
                sync(fresh)
            else:
                from kometa.sync import sync_one, sync_one_guarded
                sync_one_guarded(fresh, lambda x: sync_one(x, force=True))
        except Exception as e:
            logger.info(f"Re-match: sync for {s['title']!r} failed: {e}")
        try:
            if signals is not None:
                signals(s["id"])
            else:
                from kometa import related
                related.fill_signals(s["id"], path)
        except Exception as e:
            logger.info(f"Re-match: signals for {s['title']!r} failed: {e}")
    # Linked but EMPTY: a series tracked while Metron was away synced to zero
    # issues (The Amazing Spider-Man, tracked during the ban) and would have
    # waited for its weekly turn. Force-sync a few of those each tick too.
    if out["stopped"] is None:
        for s in empty_linked(path, limit=limit):
            try:
                if sync is not None:
                    sync(s)
                else:
                    from kometa.sync import sync_one, sync_one_guarded
                    sync_one_guarded(s, lambda x: sync_one(x, force=True))
                out["resynced"] = out.get("resynced", 0) + 1
            except metron_client.MetronUnavailable as e:
                out["stopped"] = str(e)
                break
            except Exception as e:
                logger.info(f"Re-match: empty-series sync for {s['title']!r} failed: {e}")
    return out


def empty_linked(path=None, limit: int = PER_TICK) -> list[dict]:
    """Series with a catalogue id and no issues at all — a first sync that never
    got an answer. Pull list first."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT t.* FROM tracked_series t
            WHERE (t.metron_series_id IS NOT NULL OR t.locg_series_id IS NOT NULL)
              AND (t.kind IS NULL OR t.kind != 'arc')
              AND NOT EXISTS (SELECT 1 FROM issue_status i WHERE i.tracked_series_id = t.id)
            ORDER BY t.on_pull_list DESC, t.added_at DESC LIMIT ?""", (limit,))]


# --- the one-time re-check (2026-10-10) ---------------------------------------------
# 125 series carried the 'no run to pick' flag (metron none, no LOCG id, manual)
# and nothing ever asked about them again — The Golden Child had no credits, so a
# search for its artist never found it. Most were never a human 'none': an early
# bulk pass couldn't match them. This queue asks once more: Metron first, LOCG
# only for Metron's misses (a few per tick, under the LOCG sweep's own pacing),
# exact matches linked, the rest handed to Needs matching for a person to name.
RECHECK_KEY = "recheck_queue"
LOCG_PER_TICK = 2          # lookups (each up to two searches) — the LOCG sweep's 20/tick is the real budget
LOCG_GAP_S = 3.0
NEVER = "9999-12-31 00:00:00"     # metron_checked_at for a 'no run' a person chose: never asked again


def recheck_seed(path=None) -> int:
    """Queue every flagged series once. Idempotent: a seeded queue isn't reseeded."""
    import json
    path = path or DB_PATH
    ensure_columns(path)
    have = db.get_config(path).get(RECHECK_KEY)
    if have is not None:
        return len(json.loads(have or "[]"))
    with db._connect(path) as conn:
        ids = [r[0] for r in conn.execute("""
            SELECT id FROM tracked_series
            WHERE metron_link = 'none' AND (locg_series_id IS NULL OR locg_series_id = 0)
              AND match_status = 'manual' AND COALESCE(kind, 'series') != 'arc'
              AND COALESCE(metron_checked_at, '') != ?
            ORDER BY on_pull_list DESC, title COLLATE NOCASE""", (NEVER,))]
    db.set_config({RECHECK_KEY: json.dumps(ids)}, path)
    logger.info(f"Re-check: {len(ids)} 'no run' series queued")
    return len(ids)


def _link_and_sync(s: dict, path, sync=None, signals=None):
    fresh = db.get_series_by_id(s["id"], path)
    try:
        if sync is not None:
            sync(fresh)
        else:
            from kometa.sync import sync_one, sync_one_guarded
            sync_one_guarded(fresh, lambda x: sync_one(x, force=True))
    except Exception as e:
        logger.info(f"Re-check: sync for {s['title']!r} failed: {e}")
    if signals is not None:
        signals(s["id"])
    else:
        try:
            from kometa import related
            related.fill_signals(s["id"], path)
        except Exception as e:
            logger.info(f"Re-check: signals for {s['title']!r} failed: {e}")


def recheck_tick(limit: int = PER_TICK, path=None, find_metron=None, find_locg=None, locg_open=None,
                 sync=None, signals=None, sleep=None) -> dict:
    """One tick of the queue. Metron's refusal stops the tick (the series stays
    queued); LOCG shut or out of budget leaves Metron's misses queued for later."""
    import json
    import time as _time
    path = path or DB_PATH
    sleep = sleep or _time.sleep
    from kometa import metron_client
    from kometa.shelf_import import NEEDS_MATCH
    queue = json.loads(db.get_config(path).get(RECHECK_KEY) or "[]")
    out = {"asked": 0, "metron": 0, "locg": 0, "needs_match": 0, "left": len(queue), "stopped": None}
    if not queue:
        return out
    if find_metron is None:
        from kometa.shelf_import import find_metron_match as find_metron
    if find_locg is None:
        from kometa.shelf_import import find_confident_match as find_locg
    if locg_open is None:
        from kometa.locg_client import locg_paused
        locg_open = lambda: locg_paused() is None
    done, locg_used = [], 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for sid in queue:
        if out["asked"] >= limit:
            break
        s = db.get_series_by_id(sid, path)
        if not s or s.get("metron_series_id") or s.get("locg_series_id"):
            done.append(sid)                                  # linked meanwhile (by hand, or the re-match job)
            continue
        if (s.get("metron_checked_at") or "") == NEVER:
            done.append(sid)                                  # a person said 'no run' since it was queued
            continue
        # 1. Metron, unless the re-match job asked it lately
        if not (s.get("metron_checked_at") and s["metron_checked_at"] >= cutoff) and metron_client.configured():
            try:
                mid = find_metron(s)
            except metron_client.MetronUnavailable as e:
                out["stopped"] = f"metron: {e}"
                break
            except Exception as e:
                logger.info(f"Re-check: Metron skipped {s['title']!r}: {e}")
                mid = None
            out["asked"] += 1
            with db._connect(path) as conn:
                conn.execute("UPDATE tracked_series SET metron_checked_at = ? WHERE id = ?", (_now(), sid))
            if mid:
                db.set_metron_series_id(sid, mid, path)
                db.set_metron_link(sid, "linked", path)
                db.set_match_status(sid, "auto", path)
                logger.info(f"Re-check: {s['title']!r} → Metron {mid}")
                _link_and_sync(s, path, sync, signals)
                out["metron"] += 1
                done.append(sid)
                continue
        # 2. LOCG for Metron's misses, a few per tick, never faster than the gap
        if locg_used >= LOCG_PER_TICK or not locg_open():
            continue                                          # stays queued: LOCG's turn comes later
        if locg_used:
            sleep(LOCG_GAP_S)
        locg_used += 1
        try:
            lid = find_locg(s)
        except Exception as e:
            logger.info(f"Re-check: LOCG skipped {s['title']!r}: {e}")
            continue                                          # refused or failed: stays queued
        if lid:
            db.set_locg_series_id(sid, lid, path)
            db.set_locg_link(sid, "linked", path)
            db.set_match_status(sid, "auto", path)
            logger.info(f"Re-check: {s['title']!r} → LOCG {lid}")
            _link_and_sync(s, path, sync, signals)
            out["locg"] += 1
        else:
            db.set_match_status(sid, NEEDS_MATCH, path)       # neither was sure: a person names it
            out["needs_match"] += 1
        done.append(sid)
    left = [i for i in queue if i not in done]
    db.set_config({RECHECK_KEY: json.dumps(left)}, path)
    out["left"] = len(left)
    if out["asked"] or out["locg"] or out["needs_match"] or out["metron"]:
        logger.info(f"Re-check: {out}")
    return out
