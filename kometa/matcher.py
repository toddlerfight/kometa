import re
import json
import time
import threading

import kometa.db as db

_lock = threading.Lock()
_state = {'running': False, 'done': 0, 'total': 0, 'error': None}


def get_state():
    return dict(_state)


def _normalize(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\b(the|a|an)\b\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _score(komga_title, komga_year, komga_publisher, result) -> float:
    m_title = result.get("name") or result.get("series_name") or ""
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


def _confidence(score: float) -> str:
    if score >= 0.75: return "high"
    if score >= 0.45: return "medium"
    if score >  0.15: return "low"
    return "none"


def run_scan(komga_factory, metron_factory, db_path):
    global _state
    if not _lock.acquire(blocking=False):
        return
    try:
        _state.update({"running": True, "done": 0, "total": 0, "error": None})

        komga  = komga_factory()
        metron = metron_factory()

        tracked_ids   = {s["komga_series_id"] for s in db.get_all_series(db_path)}
        candidated_ids = db.get_candidate_komga_ids(db_path)

        # Paginate through entire Komga library
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
            kid        = s["id"]
            k_title    = s["name"]
            k_pub      = s.get("metadata", {}).get("publisher")
            k_year     = s.get("metadata", {}).get("startYear")

            try:
                results = metron.search_series(k_title)
            except Exception:
                results = []

            if results:
                scored = sorted(
                    [(r, _score(k_title, k_year, k_pub, r)) for r in results[:10]],
                    key=lambda x: -x[1]
                )
                best, best_score = scored[0]
                conf    = _confidence(best_score)
                m_pub   = best.get("publisher") or {}
                m_pub   = m_pub.get("name", "") if isinstance(m_pub, dict) else str(m_pub)

                db.upsert_candidate(
                    kid, k_title, k_pub, k_year,
                    metron_id        = best.get("id"),
                    metron_title     = best.get("name") or best.get("series_name") or "",
                    metron_publisher = m_pub,
                    metron_year      = best.get("year_began"),
                    score            = best_score,
                    confidence       = conf,
                    candidates_json  = json.dumps([
                        {
                            "id":        r.get("id"),
                            "name":      r.get("name") or r.get("series_name") or "",
                            "publisher": (r.get("publisher") or {}).get("name", "") if isinstance(r.get("publisher"), dict) else "",
                            "year":      r.get("year_began"),
                            "score":     sc,
                        }
                        for r, sc in scored[:5]
                    ]),
                    path=db_path,
                )
            else:
                db.upsert_candidate(kid, k_title, k_pub, k_year, confidence="none", path=db_path)

            _state["done"] += 1
            time.sleep(0.2)

        _state["running"] = False

    except Exception as e:
        _state["running"] = False
        _state["error"] = str(e)
    finally:
        _lock.release()
