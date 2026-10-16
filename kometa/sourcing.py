"""LOCG-Only Series Credit Sourcing via Metron (Task B).

Matches series that are linked to League of Comic Geeks (locg_series_id) but have no
Metron record (metron_series_id IS NULL) and whose credits cannot be fetched from LOCG
due to Cloudflare blocks. Runs throttled background Metron searches, scores candidates
across four dimensions (title, imprint, total issue count, year), and surfaces them
in Needs Matching for explicit one-click owner confirmation.
"""
import json
import logging
from datetime import datetime, timezone
import kometa.db as db
from kometa.naming import norm_key, _pub_key
from kometa.shelf_import import _clean_title, _title_and_year
from kometa.metron_client import title_variants
import kometa.cbr as cbr

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
PER_TICK = 5

# Imprint concordance table: mapping imprint keys to canonical parent keys
IMPRINT_FAMILIES = {
    "dc": {"dc", "vertigo", "wildstorm", "blacklabel", "milestone", "americasbest"},
    "image": {"image", "skybound", "topcow", "shadowline"},
    "marvel": {"marvel", "max", "icon", "epic", "marvelknights"},
    "darkhorse": {"darkhorse", "legend"},
    "idw": {"idw", "topshelf"},
    "boom": {"boom", "boombox", "kaboom"},
}


def is_imprint_concordant(pub_a: str | None, pub_b: str | None) -> bool:
    """True if both publishers share the same imprint family."""
    ka, kb = _pub_key(pub_a or ""), _pub_key(pub_b or "")
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    for family in IMPRINT_FAMILIES.values():
        if ka in family and kb in family:
            return True
    return False


def score_title(shelf_title: str, cand_title: str) -> float:
    """Title concordance (max 40 pts).
    - 40: Exact normalized match
    - 35: Title variant match (colon/dash, leading the)
    - 25: Edition / subtitle stripped match
    - 0: Non-match
    """
    st_clean = _clean_title(shelf_title or "")
    ct_clean = _clean_title(cand_title or "")
    if not st_clean or not ct_clean:
        return 0.0

    st_raw = norm_key(st_clean)
    ct_raw = norm_key(ct_clean)
    if st_raw == ct_raw:
        return 40.0

    the = lambda k: k[4:] if k.startswith("the ") else k
    st_norm = the(st_raw)
    ct_norm = the(ct_raw)

    if st_norm == ct_norm:
        return 35.0

    variants = {the(norm_key(t)) for t in title_variants(st_clean)}
    if ct_norm in variants:
        return 35.0

    if cbr.series_slug(st_clean) == cbr.series_slug(ct_clean):
        return 25.0

    return 0.0


def score_publisher(shelf_pub: str | None, cand_pub: str | None) -> float:
    """Publisher agreement & imprint concordance (max 30 pts).
    - 30: Exact publisher match
    - 25: Imprint family concordance (e.g. Skybound vs Image)
    - 0: Unrelated publisher
    """
    ka, kb = _pub_key(shelf_pub or ""), _pub_key(cand_pub or "")
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 30.0
    if is_imprint_concordant(shelf_pub, cand_pub):
        return 25.0
    return 0.0


def score_issue_count(shelf_total: int | None, cand_count: int | None) -> float:
    """Issue count agreement (max 20 pts).
    Compares candidate count against the shelf's total (owned + missing).
    - 20: Exact count match
    - 15: Within +/- 1 issue
    - 5:  Within +/- 2 issues
    - 0:  Diff >= 3 issues
    - 10: Unknown on either side (neutral)
    """
    if shelf_total is None or cand_count is None or shelf_total <= 0 or cand_count <= 0:
        return 10.0
    diff = abs(int(cand_count) - int(shelf_total))
    if diff == 0:
        return 20.0
    if diff == 1:
        return 15.0
    if diff == 2:
        return 5.0
    return 0.0


def score_year(shelf_year: int | None, cand_year: int | None) -> float:
    """Year concordance (max 10 pts).
    - 10: Exact year match
    - 7:  Within +/- 1 year
    - 0:  Diff >= 2 years
    - 5:  Unknown on either side (neutral)
    """
    if not shelf_year or not cand_year:
        return 5.0
    diff = abs(int(cand_year) - int(shelf_year))
    if diff == 0:
        return 10.0
    if diff == 1:
        return 7.0
    return 0.0


def score_candidate(shelf_series: dict, candidate: dict, shelf_total: int | None = None) -> float:
    """Combined candidate score (0 to 100 points).
    Returns total score if >= 50, else 0.0 (discarded).
    """
    t_score = score_title(shelf_series.get("title") or "", candidate.get("title") or candidate.get("name") or "")
    if t_score <= 0.0:
        return 0.0  # Title stem must match at least stripped level

    p_score = score_publisher(shelf_series.get("publisher"), candidate.get("publisher"))
    # If publisher is completely unrelated and title is not an exact match, reject
    if p_score == 0.0 and t_score < 40.0:
        return 0.0

    i_score = score_issue_count(shelf_total, candidate.get("issue_count") or candidate.get("count"))
    y_score = score_year(shelf_series.get("year_began"), candidate.get("year"))

    total = t_score + p_score + i_score + y_score
    return round(total, 1) if total >= 50.0 else 0.0


def confidence_tier(score: float) -> str | None:
    if score >= 80.0:
        return "high"
    if score >= 50.0:
        return "moderate"
    return None


def sourcing_candidates_query(path=None, limit: int = PER_TICK) -> list[dict]:
    """Finds LOCG-only series needing credit sourcing that have not yet searched Metron."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, s.title, s.publisher, s.year_began, s.folder_path, s.locg_series_id
            FROM tracked_series s
            WHERE s.locg_series_id IS NOT NULL
              AND s.metron_series_id IS NULL
              AND s.metron_candidates_json IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.id LIMIT ?""", (limit,))]
        for r in rows:
            sid = r["id"]
            books = conn.execute("SELECT count(*) FROM books WHERE tracked_series_id = ?", (sid,)).fetchone()[0]
            owned = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,)).fetchone()[0]
            missing = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 0", (sid,)).fetchone()[0]
            r["shelf_total"] = max(books, owned + missing)
    return rows


def sourcing_tick(limit: int = PER_TICK, path=None, client=None) -> int:
    """One scheduler tick: queries Metron for candidates, scores them, and persists
    the candidate set so each series is queried exactly once.
    Stops at Metron's first refusal (shared harvest budget).
    """
    path = path or DB_PATH
    if client is None:
        from kometa import metron_client as client
    from kometa.metron_client import MetronUnavailable

    if not client.configured():
        return 0

    to_search = sourcing_candidates_query(path, limit=limit)
    if not to_search:
        return 0

    processed = 0
    with db._connect(path) as conn:
        for s in to_search:
            try:
                results = []
                for q in title_variants(s["title"]):
                    res = client.search_series(q)
                    if res:
                        results.extend(res)
            except MetronUnavailable as e:
                logger.info(f"Sourcing tick stopped: {e}")
                break
            except Exception as e:
                logger.warning(f"Sourcing search failed for {s['title']!r}: {e}")
                continue

            # Deduplicate by Metron ID
            seen_ids, scored_cands = set(), []
            for r in results:
                mid = r.get("id")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                sc = score_candidate(s, r, s.get("shelf_total"))
                tier = confidence_tier(sc)
                if tier:
                    scored_cands.append({
                        "id": mid,
                        "title": r.get("title") or r.get("name") or "",
                        "publisher": r.get("publisher") or "",
                        "year": r.get("year"),
                        "issue_count": r.get("issue_count") or r.get("count"),
                        "score": sc,
                        "confidence": tier,
                    })

            scored_cands.sort(key=lambda c: -c["score"])
            top_cands = scored_cands[:3]

            conn.execute(
                "UPDATE tracked_series SET metron_candidates_json = ? WHERE id = ?",
                (json.dumps(top_cands), s["id"]),
            )
            processed += 1
            logger.info(f"Sourcing: {s['title']!r} -> {len(top_cands)} candidate(s) stored")

    return processed
