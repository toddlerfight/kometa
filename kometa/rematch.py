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
# a run you're pulling that the catalogue hasn't got yet is one it will list soon (Batman:
# Shadow of the Bat, 2027): asked weekly, so it links within days of Metron adding it
RECHECK_DAYS_PULLING = 7
PER_TICK = 8


@db.once_per_db
def ensure_columns(path=None):
    with db._connect(path or DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
        if "metron_checked_at" not in cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN metron_checked_at TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def candidates(path=None, limit: int = PER_TICK, now: datetime | None = None) -> list[dict]:
    """Unlinked series worth asking: no Metron id, marked 'none', not asked in
    RECHECK_DAYS (RECHECK_DAYS_PULLING on the pull list). Pull list first, then alphabetical."""
    path = path or DB_PATH
    ensure_columns(path)
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_pulling = (now - timedelta(days=RECHECK_DAYS_PULLING)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM tracked_series
            WHERE metron_series_id IS NULL AND metron_link = 'none' AND (kind IS NULL OR kind != 'arc')
              AND (match_status IS NULL OR match_status != 'manual' OR from_list_id IS NOT NULL)
              AND (metron_checked_at IS NULL OR metron_checked_at < CASE WHEN on_pull_list = 1 THEN ? ELSE ? END)
            ORDER BY on_pull_list DESC, title COLLATE NOCASE""", (cutoff_pulling, cutoff))]
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


# --- the re-check, retired (2026-10-10 → 11) -----------------------------------------
# It re-asked every 'manual' + no-run series on the theory that most were an early
# bulk pass's misses. They were your Needs-matching answers, from before the NEVER
# stamp existed, and each miss went back to you to answer twice. Gone; what it took
# is handed back once below.
RECHECK_KEY = "recheck_queue"
NEVER = "9999-12-31 00:00:00"     # metron_checked_at for a 'no run' a person chose: never asked again
UNDONE_KEY = "recheck_undone"


def recheck_undo(path=None) -> int:
    """Retire the re-check and give back what it took (2026-10-11). It assumed
    the 'manual' + no-run rows were mostly an early bulk pass's; they were your
    answers from Needs matching, made before 'never ask again' was stamped, and
    every miss went back into Needs matching for you to answer twice. Once:
    those rows go back to your 'no run', stamped NEVER; the queue is emptied."""
    path = path or DB_PATH
    ensure_columns(path)
    if db.get_config(path).get(UNDONE_KEY):
        return 0
    with db._connect(path) as conn:
        n = conn.execute("""UPDATE tracked_series SET match_status = 'manual', metron_checked_at = ?
            WHERE match_status = 'needs_match' AND metron_link = 'none'
              AND (locg_series_id IS NULL OR locg_series_id = 0) AND metron_series_id IS NULL""", (NEVER,)).rowcount
        conn.execute("""UPDATE tracked_series SET metron_checked_at = ?
            WHERE match_status = 'manual' AND metron_link = 'none' AND from_list_id IS NULL
              AND metron_series_id IS NULL AND (locg_series_id IS NULL OR locg_series_id = 0)""", (NEVER,))
    db.set_config({RECHECK_KEY: "[]", UNDONE_KEY: _now()}, path)
    logger.info(f"Re-check retired: {n} series handed back to your 'no run'")
    return n
