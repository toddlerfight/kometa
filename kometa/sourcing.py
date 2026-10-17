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
YEARS_BEFORE_MAX = 10           # a candidate run may begin at most this long before the shelf run


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
    # A run that began decades before yours isn't yours (Batman: Shadow of the Bat, 2027:
    # the card offered the 1992 run). Later is still possible — a collected edition.
    if shelf_year and cand_year and int(shelf_year) - int(cand_year) > YEARS_BEFORE_MAX:
        return 0.0
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


# --- the files' own word: year and writer ------------------------------------------------
# 2026-10-17: the cards compared titles and issue counts and never looked inside the files,
# so they offered the 1968 Captain Marvel beside the 2019 run the files are (Kelly Thompson,
# 2019) and three wrong Thors. Reading the files' year and writer settled 15 of 20 by hand.
# Now the matcher asks each candidate run for the files' own issue: a run whose issue came
# out in another year isn't this comic and leaves the card; when exactly one run agrees on
# year AND writer, it's linked with no card at all.
EVIDENCE_BOOKS = 3
YEAR_SLACK = 1                      # ComicInfo's Year is the cover date; store dates run ahead of it
_WRITER_ROLES = {"writer", "story", "script", "plot"}


def file_evidence(series_id: int, path=None, read=None, limit: int = EVIDENCE_BOOKS) -> list[dict]:
    """[{number, year, writers}] from the ComicInfo of the run's first numbered books that carry a year."""
    from kometa import covers
    path = path or DB_PATH
    read = read or covers.read_comicinfo
    with db._connect(path) as conn:
        books = conn.execute("SELECT path, number FROM books WHERE tracked_series_id = ? AND number IS NOT NULL "
                             "ORDER BY number", (series_id,)).fetchall()
    out = []
    for b in books:
        ci = read(b["path"]) or {}
        y = (ci.get("Year") or "").strip()
        if not y.isdigit():
            continue
        writers = [w for w in re.split(r"\s*[,;]\s*", ci.get("Writer") or "") if w.strip()]
        out.append({"number": float(b["number"]), "year": int(y), "writers": writers})
        if len(out) >= limit:
            break
    return out


def _issue_year(issue: dict) -> int | None:
    d = issue.get("cover_date") or issue.get("store_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


def check_with_files(evidence: list[dict], cands: list[dict], client) -> tuple[int | None, list[dict]]:
    """Each candidate asked for the files' first issue (one Metron call, plus one for its
    credits when the year fits). Returns (the one run that agrees on year and writer, or
    None; the candidates still in the running, each tagged `files`: agree / year /
    unknown / none). A run whose issue is from another year is dropped. Raises MetronUnavailable."""
    if not evidence:
        return None, [dict(c, files="none") for c in cands]
    ev = evidence[0]
    mine = {norm_key(w) for w in ev["writers"]}
    kept, agree = [], []
    for c in cands:
        rows = client._get("issue/", series_id=c["id"], number=f"{ev['number']:g}").get("results") or []
        if not rows:
            kept.append(dict(c, files="unknown"))          # the catalogue hasn't that number: can't tell
            continue
        y = _issue_year(rows[0])
        if y is not None and abs(y - ev["year"]) > YEAR_SLACK:
            continue                                        # another run's issue: not this comic
        tag = "unknown" if y is None else "year"
        if y is not None and mine and rows[0].get("id"):
            credits = client.issue_detail(rows[0]["id"]).get("credits") or []
            theirs = {norm_key(x.get("name") or "") for x in credits if (x.get("role") or "").lower() in _WRITER_ROLES}
            if mine & theirs:
                tag = "agree"
                agree.append(c)
        kept.append(dict(c, files=tag))
    return (agree[0]["id"] if len(agree) == 1 else None), kept


def _settle(s: dict, cands: list[dict], path, client) -> tuple[int | None, list[dict]]:
    """The file check on one series' candidates: (the run to link or None, what's left)."""
    return check_with_files(file_evidence(s["id"], path), cands, client)


def _store(s: dict, link: int | None, cands: list[dict], path) -> None:
    with db._connect(path) as conn:
        conn.execute("UPDATE tracked_series SET metron_candidates_json = ? WHERE id = ?", (json.dumps(cands), s["id"]))
        if link:
            conn.execute("UPDATE tracked_series SET metron_series_id = ? WHERE id = ?", (link, s["id"]))
    if link:
        logger.info(f"Sourcing: {s['title']!r} linked to Metron {link} — its files' year and writer agree")


def verify_deck(path=None, client=None, limit: int = PER_TICK) -> int:
    """Cards stored before the file check: checked once each, the same way. Returns how many."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT id, title, metron_candidates_json FROM tracked_series
            WHERE metron_series_id IS NULL AND metron_candidates_json IS NOT NULL AND metron_candidates_json != '[]'
              AND (locg_link IS NULL OR locg_link != 'none')
              AND (metron_checked_at IS NULL OR metron_checked_at != '9999-12-31 00:00:00')""")]
    todo = []
    for r in rows:
        cands = json.loads(r["metron_candidates_json"] or "[]")
        if cands and any("files" not in c for c in cands):
            todo.append((r, cands))
    done = 0
    for r, cands in todo[:limit]:
        _store(r, *_settle(r, cands, path, client), path)
        done += 1
    return done


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
    """One scheduler tick: queries Metron for candidates, scores them, checks them against
    the files, and persists the candidate set so each series is queried exactly once.
    Stops at Metron's first refusal (shared harvest budget). Every Metron call happens
    outside a write: each series' result is written in a short transaction of its own.
    """
    path = path or DB_PATH
    if client is None:
        from kometa import metron_client as client
    from kometa.metron_client import MetronUnavailable

    if not client.configured():
        return 0

    ensure_deck_rescored(path)

    processed = 0
    for s in sourcing_candidates_query(path, limit=limit):
        try:
            results = []
            for q in title_variants(s["title"]):
                res = client.search_series(q)
                if res:
                    results.extend(res)
        except MetronUnavailable as e:
            logger.info(f"Sourcing tick stopped: {e}")
            return processed
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
        try:
            link, top_cands = _settle(s, scored_cands[:3], path, client)
        except MetronUnavailable as e:
            logger.info(f"Sourcing tick stopped at the file check: {e}")
            return processed                            # not stored: searched again next tick
        _store(s, link, top_cands, path)
        processed += 1
        logger.info(f"Sourcing: {s['title']!r} -> {len(top_cands)} candidate(s) stored")

    try:
        verify_deck(path, client)
    except MetronUnavailable as e:
        logger.info(f"Sourcing deck check stopped: {e}")
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
                        **({"files": c["files"]} if "files" in c else {}),     # the file check stands
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
    if not cfg.get("sourcing_rescore_v3"):          # v3: a run decades before yours leaves the card
        rescore_deck(path)
        db.set_config({"sourcing_rescore_v2": "1", "sourcing_rescore_v3": "1"}, path)
