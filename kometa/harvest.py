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
