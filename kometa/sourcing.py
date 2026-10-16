"""LOCG-Only Series Credit Sourcing via Metron (Task B).

Matches series that are linked to League of Comic Geeks (locg_series_id) but have no
Metron record (metron_series_id IS NULL) and whose credits cannot be fetched from LOCG
due to Cloudflare blocks. Runs throttled background Metron searches, scores candidates
across four dimensions (title, imprint, total issue count, year), and surfaces them
in Needs Matching for explicit one-click owner confirmation.
"""
import json
import logging
import re
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


COLLECTED_EDITION_RE = re.compile(
    r"\b(hc|hardcover|tpb|trade\s+paperback|omnibus|deluxe\s+edition|anniversary\s+edition|collected\s+edition|compendium|graphic\s+novel|gn)\b",
    re.IGNORECASE,
)


def is_collected_edition(title: str | None) -> bool:
    """True if title indicates a collected edition (HC, TPB, Omnibus, etc)."""
    return bool(COLLECTED_EDITION_RE.search(title or ""))


EDITION_PENALTY = 15.0


def score_title(shelf_title: str, cand_title: str, is_single_book: bool = False) -> float:
    """Title concordance (max 40 pts).
    - 40: Exact normalized match
    - 35: Title variant match (colon/dash, leading the)
    - 25: Edition / subtitle stripped match
    - Edition penalty (-15 pts): applied if candidate is a collected edition (HC/TPB/Omnibus)
      while the shelf title is not, unless the shelf run is a single book (e.g. GN).
    - 0: Non-match
    """
    st_clean = _clean_title(shelf_title or "")
    ct_clean = _clean_title(cand_title or "")
    if not st_clean or not ct_clean:
        return 0.0

    score = 0.0
    st_raw = norm_key(st_clean)
    ct_raw = norm_key(ct_clean)
    if st_raw == ct_raw:
        score = 40.0
    else:
        the = lambda k: k[4:] if k.startswith("the ") else k
        st_norm = the(st_raw)
        ct_norm = the(ct_raw)

        if st_norm == ct_norm:
            score = 35.0
        else:
            variants = {the(norm_key(t)) for t in title_variants(st_clean)}
            if ct_norm in variants:
                score = 35.0
            elif cbr.series_slug(st_clean) == cbr.series_slug(ct_clean):
                score = 25.0

    if score > 0.0 and is_collected_edition(cand_title) and not is_collected_edition(shelf_title):
        if not is_single_book:
            score -= EDITION_PENALTY

    return max(score, 0.0)


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
    # 1. Candidate issue count cannot be below the highest owned issue number
    cand_count = candidate.get("issue_count") or candidate.get("count")
    max_owned = shelf_series.get("max_owned_issue")
    if max_owned is not None and cand_count is not None:
        try:
            if float(cand_count) < float(max_owned):
                return 0.0  # Run cannot have fewer issues than an owned issue number
        except (ValueError, TypeError):
            pass

    # Check if shelf run is a single book (e.g. Graphic Novel, one-shot)
    owned_count = shelf_series.get("owned_count")
    shelf_total = shelf_total if shelf_total is not None else shelf_series.get("shelf_total")
    is_single_book = (
        (owned_count == 1 and (max_owned is None or max_owned <= 1.0))
        or (shelf_total == 1 and (max_owned is None or max_owned <= 1.0))
    )

    t_score = score_title(
        shelf_series.get("title") or "",
        candidate.get("title") or candidate.get("name") or "",
        is_single_book=is_single_book,
    )
    if t_score <= 0.0:
        return 0.0  # Title stem must match at least stripped level

    p_score = score_publisher(shelf_series.get("publisher"), candidate.get("publisher"))
    # If publisher is completely unrelated and title is not an exact match, reject
    if p_score == 0.0 and t_score < 40.0:
        return 0.0

    i_score = score_issue_count(shelf_total, cand_count)

    shelf_year = shelf_series.get("year_began")
    if not shelf_year:
        m = re.search(r"\((\d{4})\)", shelf_series.get("title") or "")
        if m:
            shelf_year = int(m.group(1))

    cand_year = candidate.get("year")
    y_score = score_year(shelf_year, cand_year)

    total = t_score + p_score + i_score + y_score
    if total < 50.0:
        return 0.0

    # When the shelf run has no year, don't award full confidence (>= 80) on title + publisher alone.
    # Cap at moderate (75.0) unless year or issue range pins it.
    if not shelf_year:
        pinned_by_range = False
        if cand_count is not None:
            try:
                c_cnt = int(cand_count)
                if is_single_book and c_cnt == 1:
                    pinned_by_range = True
                elif owned_count is not None and owned_count >= 3 and abs(int(owned_count) - c_cnt) <= 1:
                    pinned_by_range = True
            except (ValueError, TypeError):
                pass
        if not pinned_by_range:
            total = min(total, 75.0)

    return round(total, 1)


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
              AND (s.locg_link IS NULL OR s.locg_link != 'none')
              AND (s.metron_checked_at IS NULL OR s.metron_checked_at != '9999-12-31 00:00:00')
            ORDER BY s.id LIMIT ?""", (limit,))]
        for r in rows:
            sid = r["id"]
            books = conn.execute("SELECT count(*) FROM books WHERE tracked_series_id = ?", (sid,)).fetchone()[0]
            owned = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,)).fetchone()[0]
            missing = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 0", (sid,)).fetchone()[0]
            max_owned = conn.execute("SELECT MAX(number) FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,)).fetchone()[0]
            r["shelf_total"] = max(books, owned + missing)
            r["owned_count"] = owned if owned > 0 else books
            r["max_owned_issue"] = max_owned
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

    ensure_deck_rescored(path)

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
                    cand_title = r.get("title") or r.get("name") or ""
                    owned_count = s.get("owned_count") or 0
                    max_owned = s.get("max_owned_issue")
                    shelf_total = s.get("shelf_total")
                    is_single_book = (
                        (owned_count == 1 and (max_owned is None or max_owned <= 1.0))
                        or (shelf_total == 1 and (max_owned is None or max_owned <= 1.0))
                    )
                    is_ed = is_collected_edition(cand_title) and not is_collected_edition(s.get("title")) and not is_single_book
                    scored_cands.append({
                        "id": mid,
                        "title": cand_title,
                        "publisher": r.get("publisher") or "",
                        "year": r.get("year"),
                        "issue_count": r.get("issue_count") or r.get("count"),
                        "score": sc,
                        "confidence": tier,
                        "is_edition": is_ed,
                    })

            # Regular series matches sort before collected editions, then by score descending
            scored_cands.sort(key=lambda c: (c.get("is_edition", False), -c["score"]))
            top_cands = scored_cands[:3]

            conn.execute(
                "UPDATE tracked_series SET metron_candidates_json = ? WHERE id = ?",
                (json.dumps(top_cands), s["id"]),
            )
            processed += 1
            logger.info(f"Sourcing: {s['title']!r} -> {len(top_cands)} candidate(s) stored")

    return processed


def rescore_deck(path=None) -> int:
    """Recomputes scores for all candidate cards already in the deck without re-querying Metron."""
    path = path or DB_PATH
    updated = 0
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT id, title, publisher, year_began, folder_path, metron_candidates_json
            FROM tracked_series
            WHERE metron_candidates_json IS NOT NULL
              AND metron_candidates_json != '[]'
              AND metron_series_id IS NULL
        """).fetchall()]
        for s in rows:
            sid = s["id"]
            books = conn.execute("SELECT count(*) FROM books WHERE tracked_series_id = ?", (sid,)).fetchone()[0]
            owned = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,)).fetchone()[0]
            missing = conn.execute("SELECT count(*) FROM issue_status WHERE tracked_series_id = ? AND owned = 0", (sid,)).fetchone()[0]
            max_owned = conn.execute("SELECT MAX(number) FROM issue_status WHERE tracked_series_id = ? AND owned = 1", (sid,)).fetchone()[0]
            s["shelf_total"] = max(books, owned + missing)
            s["owned_count"] = owned if owned > 0 else books
            s["max_owned_issue"] = max_owned

            try:
                cands = json.loads(s.get("metron_candidates_json") or "[]")
            except Exception:
                cands = []
            if not cands:
                continue

            scored_cands = []
            is_single_book = (
                (s["owned_count"] == 1 and (max_owned is None or max_owned <= 1.0))
                or (s["shelf_total"] == 1 and (max_owned is None or max_owned <= 1.0))
            )

            for c in cands:
                sc = score_candidate(s, c, s["shelf_total"])
                tier = confidence_tier(sc)
                if tier:
                    cand_title = c.get("title") or c.get("name") or ""
                    is_ed = is_collected_edition(cand_title) and not is_collected_edition(s.get("title")) and not is_single_book
                    scored_cands.append({
                        "id": c["id"],
                        "title": cand_title,
                        "publisher": c.get("publisher") or "",
                        "year": c.get("year"),
                        "issue_count": c.get("issue_count") or c.get("count"),
                        "score": sc,
                        "confidence": tier,
                        "is_edition": is_ed,
                    })

            scored_cands.sort(key=lambda c: (c.get("is_edition", False), -c["score"]))
            top_cands = scored_cands[:3]
            conn.execute(
                "UPDATE tracked_series SET metron_candidates_json = ? WHERE id = ?",
                (json.dumps(top_cands), sid),
            )
            updated += 1
    return updated


def ensure_deck_rescored(path=None):
    """Ensures existing candidate cards in the deck are re-scored once with v2 rules."""
    path = path or DB_PATH
    cfg = db.get_config(path)
    if not cfg.get("sourcing_rescore_v2"):
        rescore_deck(path)
        db.set_config({"sourcing_rescore_v2": "1"}, path)
