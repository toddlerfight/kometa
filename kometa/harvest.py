"""The library harvest at a glance (docs/library-enrichment-spec.md, 1b/1c).

The harvest isn't one job: it's the trickles that were already there — the
record fill (Metron, LOCG when open), the LOCG variant sweep, the LOCG top-up
queue, the image store. Each keeps its own pace and its own refusals. This is
the one place that adds them up, so 'how far along are we' is a single read
instead of five log greps.
"""
import time

from fastapi import APIRouter

import kometa.db as db

router = APIRouter()
DB_PATH = db.DB_PATH
_IMAGES_TTL_S = 120
_images_cache = {"at": 0.0, "n": None}


def _owned_record(path) -> dict:
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT
              SUM(CASE WHEN r.source IN ('metron', 'locg') AND r.fill_state != 'miss' THEN 1 ELSE 0 END) AS catalogue,
              SUM(CASE WHEN r.source IN ('comicinfo', 'komga') THEN 1 ELSE 0 END) AS stopgap,
              SUM(CASE WHEN r.tracked_series_id IS NULL OR r.fill_state = 'miss' THEN 1 ELSE 0 END) AS no_record,
              SUM(CASE WHEN i.metron_issue_id IS NULL AND (i.locg_issue_id IS NULL OR i.locg_issue_id = '') THEN 1 ELSE 0 END) AS no_catalogue_id,
              SUM(CASE WHEN r.variants_at IS NOT NULL THEN 1 ELSE 0 END) AS variant_lists,
              COUNT(*) AS owned
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND (s.kind IS NULL OR s.kind != 'arc')""").fetchone()
    return {k: rows[k] or 0 for k in rows.keys()}


def _images_pending(path) -> int | None:
    """The image store's queue is computed, not stored — cached briefly, it walks
    every variant list the record knows."""
    if time.time() - _images_cache["at"] < _IMAGES_TTL_S:
        return _images_cache["n"]
    try:
        from kometa import images
        n = len(images.pending(path, limit=100_000))
    except Exception:
        n = None
    _images_cache.update(at=time.time(), n=n)
    return n


def progress(path=None) -> dict:
    path = path or DB_PATH
    from kometa import metron_client, record, topup
    out = {"owned_issues": _owned_record(path), "record": record.backlog(path),
           "metron": metron_client.budget(), "images_pending": _images_pending(path)}
    try:
        out["locg"] = topup.status(path)
    except Exception:
        out["locg"] = None
    return out


@router.get("/api/harvest/progress")
def get_progress():
    return progress()


# --- issue ids for runs matched after their last sync (1d) -----------------------
IDS_PER_TICK = 3


def unfetched_series(path=None, limit: int = IDS_PER_TICK) -> list[dict]:
    """Runs matched to Metron whose issue list was never fetched — linked after
    their last sync, then left for the weekly not-pulled cycle (2026-10-12: 27
    runs, 517 owned issues, Hellblazer among them, with no issue ids at all).
    Most owned issues first."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        ids = [r[0] for r in conn.execute("""
            SELECT s.id FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.metron_series_id IS NOT NULL AND s.metron_fetched_at IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc') AND i.owned = 1
            GROUP BY s.id ORDER BY COUNT(*) DESC, s.id LIMIT ?""", (limit,))]
    return [s for s in (db.get_series_by_id(i, path) for i in ids) if s]


def issue_ids_tick(limit: int = IDS_PER_TICK, path=None, sync=None) -> int:
    """Scheduler tick (harvest traffic): sync a few of those runs through the one
    sync path — its Metron list carries the ids, its folder scan keeps ownership
    honest. Stops at Metron's first refusal (the harvest floor included)."""
    from kometa import metron_client
    from kometa.sync import sync_one_guarded
    done = 0
    for s in unfetched_series(path, limit):
        if not metron_client.configured() or metron_client.harvest_share_spent():
            break
        (sync or sync_one_guarded)(s)
        done += 1
    return done


# --- runs that may span more than one Metron series (db.metron_series_ids) -------
def split_candidates(path=None, min_unmatched: int = 25, min_outside: int = 3) -> dict:
    """Local data only, no catalogue calls. 'outside': matched, but owned issues
    run past the Metron run's numbers (Fables #150–162 beyond a run that ends at
    #149) — a continuation under another Metron series, or a wrong match.
    'unmatched': no Metron run and a long one on the shelf (Fantastic Four
    #61–611). Suggestions for a person to confirm; nothing is linked from here."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, s.title, s.publisher, s.metron_series_id, s.metron_extra_ids,
                   SUM(i.owned) AS owned,
                   SUM(CASE WHEN i.owned = 1 AND i.metron_issue_id IS NULL THEN 1 ELSE 0 END) AS owned_no_id,
                   MIN(CASE WHEN i.owned = 1 THEN i.number END) AS owned_lo, MAX(CASE WHEN i.owned = 1 THEN i.number END) AS owned_hi,
                   MIN(CASE WHEN i.metron_issue_id IS NOT NULL THEN i.number END) AS metron_lo,
                   MAX(CASE WHEN i.metron_issue_id IS NOT NULL THEN i.number END) AS metron_hi
            FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.kind IS NULL OR s.kind != 'arc'
            GROUP BY s.id HAVING owned > 0""")]
    outside = [r for r in rows if r["metron_series_id"] and r["metron_lo"] is not None and r["owned_no_id"] >= min_outside
               and (r["owned_lo"] < r["metron_lo"] or r["owned_hi"] > r["metron_hi"])]
    unmatched = [r for r in rows if not r["metron_series_id"] and r["owned"] >= min_unmatched]
    return {"outside": sorted(outside, key=lambda r: -r["owned_no_id"]),
            "unmatched": sorted(unmatched, key=lambda r: -r["owned"])}


@router.get("/api/harvest/split-candidates")
def get_split_candidates():
    return split_candidates()
