import re
import json
import time
import logging
import threading
from collections import deque

logger = logging.getLogger(__name__)

import kometa.db as db

_lock           = threading.Lock()
_state          = {'running': False, 'done': 0, 'total': 0, 'auto_confirmed': 0, 'error': None}
_recent: deque  = deque(maxlen=50)   # last N results for live feed
_sync_callback  = None               # injected by start(); called after scan if series were auto-confirmed


def get_state():
    return {**_state, 'recent': list(_recent)}


def start(komga_factory, metron_factory, db_path, sync_callback=None, retry_empty=False) -> bool:
    """Acquire lock + set running=True in the calling thread, then spawn worker.
    retry_empty=True resets none-confidence empty candidates so they get re-scanned.
    Returns False if already running."""
    global _sync_callback
    if not _lock.acquire(blocking=False):
        return False
    _sync_callback = sync_callback
    if retry_empty:
        _reset_empty_candidates(db_path)
    _state.update({'running': True, 'done': 0, 'total': 0, 'auto_confirmed': 0, 'error': None})
    _recent.clear()
    t = threading.Thread(target=_run, args=(komga_factory, metron_factory, db_path), daemon=True)
    t.start()
    return True


def _reset_empty_candidates(db_path):
    """Delete none-confidence candidates that got no API results (rate-limit victims)."""
    import kometa.db as _db
    with _db._connect(db_path) as conn:
        conn.execute("""
            DELETE FROM match_candidates
            WHERE confidence = 'none'
              AND (candidates_json IS NULL OR candidates_json = '[]' OR candidates_json = '')
        """)


def _normalize(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"^\[.*?\]\s*", "", t)       # strip leading [year] or [tag] prefixes
    t = re.sub(r"^\(\d{4}\)\s*", "", t)     # strip leading (year) prefixes
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)  # strip trailing (year) — Metron's disambiguator
    t = re.sub(r"[/\-:&]", " ", t)          # separators → space (preserves word gaps)
    t = re.sub(r"[^\w\s]", "", t)           # strip remaining punctuation
    t = re.sub(r"\b(the|a|an)\b\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _score(komga_title, komga_year, komga_publisher, result) -> float:
    m_title = result.get("series") or result.get("name") or result.get("series_name") or ""
    m_year  = result.get("year_began")
    m_pub   = result.get("publisher") or {}
    m_pub   = m_pub.get("name", "") if isinstance(m_pub, dict) else str(m_pub)

    kn, mn = _normalize(komga_title), _normalize(m_title)
    if not kn or not mn:
        return 0.0

    score = 0.0
    if kn == mn:
        score += 0.6
    elif kn in mn or mn in kn:
        score += 0.35
    else:
        kw, mw = set(kn.split()), set(mn.split())
        if kw and mw:
            score += (len(kw & mw) / max(len(kw), len(mw))) * 0.3

    if komga_year and m_year:
        diff = abs(int(komga_year) - int(m_year))
        score += 0.25 if diff <= 1 else (0.1 if diff <= 3 else 0)

    if komga_publisher and m_pub:
        kp, mp = _normalize(komga_publisher), _normalize(m_pub)
        if kp == mp:
            score += 0.15
        elif kp in mp or mp in kp:
            score += 0.08

    return round(score, 3)


def _confidence(score: float, gap: float) -> str:
    """gap = best_score - second_best_score (0 if only one result)."""
    # Clear winner: good score and nothing close behind it
    if score >= 0.55 and gap >= 0.20:  return "high"
    # Single result with a solid score is also fine
    if score >= 0.55 and gap == score: return "high"
    # Ambiguous — multiple plausible candidates or borderline score
    if score >= 0.35: return "medium"
    if score >  0.15: return "low"
    return "none"


def _metron_search(metron, query: str, max_attempts: int = 3) -> list:
    """Search Metron with rate-limit sleep and retry backoff."""
    for attempt in range(max_attempts):
        if attempt:
            wait = 2 ** attempt  # 2s, 4s
            logger.warning(f"Metron search retry {attempt}/{max_attempts-1} for {query!r} (waiting {wait}s)")
            time.sleep(wait)
        try:
            results = metron.search_series(query)
            time.sleep(0.4)  # respect Metron's rate limit between every search
            if results is not None:
                return results
        except Exception as e:
            logger.warning(f"Metron search failed for {query!r}: {e}")
    logger.warning(f"Metron search gave up for {query!r}")
    return []


def _run(komga_factory, metron_factory, db_path):
    try:
        komga  = komga_factory()
        metron = metron_factory()

        tracked_ids    = {s["komga_series_id"] for s in db.get_all_series(db_path)}  # mutable — updated on auto-confirm
        candidated_ids = db.get_candidate_komga_ids(db_path)

        all_series, page = [], 0
        while True:
            data = komga._get("/api/v1/series", params={"page": page, "size": 100, "sort": "metadata.titleSort,asc"})
            all_series.extend(data.get("content", []))
            if data.get("last", True):
                break
            page += 1

        to_scan = [s for s in all_series
                   if s["id"] not in tracked_ids and s["id"] not in candidated_ids]

        _state["total"] = len(to_scan)

        for s in to_scan:
            kid     = s["id"]
            k_title = s["name"]
            k_pub   = s.get("metadata", {}).get("publisher")
            k_year  = s.get("metadata", {}).get("startYear")

            # Clean search query for Metron: strip prefixes, replace separators
            search_title = re.sub(r"^\[.*?\]\s*", "", k_title).strip()
            search_title = re.sub(r"^\(\d{4}\)\s*", "", search_title).strip()
            search_title = re.sub(r"^[-–]\s*", "", search_title).strip()    # strip leading dash (folder artefact)
            search_title = re.sub(r"\s*[-–]\s*", " ", search_title)         # all dashes → space
            search_title = re.sub(r"[/:&,]", " ", search_title)             # separators → space
            search_title = re.sub(r"[!?%#@$\^*'\"()]", "", search_title)    # strip chars that break Metron search
            search_title = re.sub(r"\s+", " ", search_title).strip() or k_title

            results = _metron_search(metron, search_title)

            if results:
                scored = sorted(
                    [(r, _score(k_title, k_year, k_pub, r)) for r in results],
                    key=lambda x: -x[1],
                )
                best, best_score = scored[0]
                second_score     = scored[1][1] if len(scored) > 1 else 0.0
                gap              = round(best_score - second_score, 3)
                conf             = _confidence(best_score, gap)

                m_pub = best.get("publisher") or {}
                m_pub = m_pub.get("name", "") if isinstance(m_pub, dict) else str(m_pub)
                m_title = best.get("series") or best.get("name") or best.get("series_name") or ""
                m_id    = best.get("id")
                m_year  = best.get("year_began")

                db.upsert_candidate(
                    kid, k_title, k_pub, k_year,
                    metron_id        = m_id,
                    metron_title     = m_title,
                    metron_publisher = m_pub,
                    metron_year      = m_year,
                    score            = best_score,
                    confidence       = conf,
                    candidates_json  = json.dumps([
                        {
                            "id":          r.get("id"),
                            "name":        r.get("series") or r.get("name") or r.get("series_name") or "",
                            "publisher":   (r.get("publisher") or {}).get("name", "") if isinstance(r.get("publisher"), dict) else "",
                            "year":        r.get("year_began"),
                            "issue_count": r.get("issue_count"),
                            "volume":      r.get("volume"),
                            "score":       sc,
                        }
                        for r, sc in scored[:5]
                    ]),
                    path=db_path,
                )

                # Auto-confirm high-confidence matches without waiting for user
                if conf == "high" and kid not in tracked_ids:
                    try:
                        db.add_series(
                            kid, m_id,
                            title      = k_title,
                            publisher  = k_pub,
                            year_began = m_year,
                            path       = db_path,
                        )
                        db.confirm_candidate(kid, m_id, db_path)
                        tracked_ids.add(kid)
                        _state["auto_confirmed"] += 1
                    except Exception:
                        pass  # already tracked or DB error — leave as pending

                _recent.appendleft({
                    "komga_id":   kid,
                    "title":      k_title,
                    "match":      m_title or None,
                    "confidence": conf,
                    "score":      best_score,
                    "candidates": [
                        {
                            "id":          r.get("id"),
                            "name":        r.get("series") or r.get("name") or r.get("series_name") or "",
                            "year":        r.get("year_began"),
                            "issue_count": r.get("issue_count"),
                            "volume":      r.get("volume"),
                            "score":       sc,
                        }
                        for r, sc in scored[:5]
                    ],
                })
            else:
                db.upsert_candidate(kid, k_title, k_pub, k_year, confidence="none", path=db_path)
                _recent.appendleft({
                    "komga_id":   kid,
                    "title":      k_title,
                    "match":      None,
                    "confidence": "none",
                    "score":      0,
                    "candidates": [],
                })

            _state["done"] += 1
            time.sleep(0.2)

        # Kick off issue sync for all auto-confirmed series in background
        if _state["auto_confirmed"] and _sync_callback:
            threading.Thread(target=_sync_callback, daemon=True).start()

        _state["running"] = False

    except Exception as e:
        _state["running"] = False
        _state["error"] = str(e)
    finally:
        _lock.release()
