"""Recommendations — built on the creator signal mined from LOCG issue details.

Slice 1: the taste profile. For each tracked series we look at a representative
issue's creators (cached in issue_details_cache), then aggregate across the
library to find the creators you collect most. That profile is the basis for
candidate generation later.

Editorial/production roles are ignored — they don't signal taste. A creator only
counts once per series (being on 8 issues of one book isn't 8 signals).
"""
import re
import time
import logging

import kometa.db as db
from kometa.naming import _pub_key
from kometa.locg_client import get_issue_details_anon, get_creator_series_anon

logger = logging.getLogger(__name__)

# LOCG rate-limits hard; throttle enrichment and back off on 429.
_ENRICH_DELAY = 1.5
_ENRICH_RETRIES = 3

# Reprints / promos / format variants — not real "new series to discover".
_NOISE_RE = re.compile(
    r"\b(omnibus|ashcan|deluxe|edition|compendium|sampler|preview|fcbd|"
    r"director'?s? cut|tpb|hardcover|hc|giant|showcase|free comic book day|"
    r"day 20\d\d|noir|facsimile)\b", re.I
)

# Roles that don't signal reader taste (editors, letterers, production, etc.)
_EXCLUDE_ROLE_RE = re.compile(r"editor|letter|production|publisher|designer|translat", re.I)


def _representative_issue(series_id, path):
    """One issue to read a series' creative team from — the lowest-numbered issue
    that has a LOCG id (usually #1, where the core team is established)."""
    issues = [i for i in db.get_issues_for_series(series_id, path) if i.get("locg_issue_id")]
    return min(issues, key=lambda i: i["number"]) if issues else None


def _series_creators(series_id, path):
    """Creative creators for a series (from its representative issue's cache).
    Returns [{people_id, name, people_slug, role}] — editorial roles filtered out."""
    rep = _representative_issue(series_id, path)
    if not rep:
        return []
    cache = db.get_issue_details_cache(rep["locg_issue_id"], path)
    if not cache:
        return []
    return [
        c for c in cache.get("credits", [])
        if c.get("people_id") and not _EXCLUDE_ROLE_RE.search(c.get("role", ""))
    ]


def enrich_library(path=db.DB_PATH) -> dict:
    """Ensure every tracked series has its representative issue's details cached.
    Hits LOCG only for what's missing (the cache is permanent). Returns counts."""
    series = db.get_all_series(path)
    fetched = skipped = failed = 0
    for s in series:
        rep = _representative_issue(s["id"], path)
        if not rep:
            skipped += 1
            continue
        if db.get_issue_details_cache(rep["locg_issue_id"], path) is not None:
            skipped += 1
            continue
        for attempt in range(_ENRICH_RETRIES):
            try:
                detail = get_issue_details_anon(rep["locg_issue_id"])
                db.set_issue_details_cache(rep["locg_issue_id"], detail, path)
                fetched += 1
                break
            except Exception as e:
                if "429" in str(e) and attempt < _ENRICH_RETRIES - 1:
                    time.sleep(5 * (attempt + 1))  # back off, then retry
                    continue
                failed += 1
                logger.warning(f"enrich failed for {s.get('title')!r}: {e}")
                break
        time.sleep(_ENRICH_DELAY)  # throttle so we don't trip the rate limit
    return {"total": len(series), "fetched": fetched, "cached": skipped, "failed": failed}


def taste_profile(path=db.DB_PATH, limit=20, min_series=2) -> list[dict]:
    """Aggregate cached creators across the library into your top creators —
    ranked by how many of your series they're on (writers break ties)."""
    creators = {}  # people_id -> aggregate
    for s in db.get_all_series(path):
        for c in _series_creators(s["id"], path):
            e = creators.setdefault(c["people_id"], {
                "people_id": c["people_id"], "name": c["name"],
                "slug": c.get("people_slug"), "series": set(), "roles": set(),
            })
            e["series"].add(s["title"])
            e["roles"].add(c["role"])
            if c.get("people_slug"):
                e["slug"] = c["people_slug"]

    ranked = sorted(
        creators.values(),
        key=lambda e: (len(e["series"]), "Writer" in e["roles"]),
        reverse=True,
    )
    return [
        {
            "people_id": e["people_id"], "name": e["name"], "slug": e["slug"],
            "series_count": len(e["series"]),
            "series": sorted(e["series"]),
            "roles": sorted(e["roles"]),
        }
        for e in ranked if len(e["series"]) >= min_series
    ][:limit]


def enrich_creators(path=db.DB_PATH, top=15) -> dict:
    """Cache each top creator's catalog (their other series). Throttled + 429-backoff."""
    prof = taste_profile(path, limit=top)
    fetched = skipped = failed = 0
    for c in prof:
        if not c.get("slug"):
            skipped += 1
            continue
        if db.get_creator_works_cache(c["people_id"], path) is not None:
            skipped += 1
            continue
        for attempt in range(_ENRICH_RETRIES):
            try:
                works = get_creator_series_anon(c["people_id"], c["slug"])
                db.set_creator_works_cache(c["people_id"], works, path)
                fetched += 1
                break
            except Exception as e:
                if "429" in str(e) and attempt < _ENRICH_RETRIES - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                failed += 1
                logger.warning(f"creator works fetch failed for {c['name']!r}: {e}")
                break
        time.sleep(_ENRICH_DELAY)
    return {"creators": len(prof), "fetched": fetched, "cached": skipped, "failed": failed}


def recommendations(path=db.DB_PATH, limit=20, top_creators=15) -> list[dict]:
    """Series you don't track, by the creators you collect most. Ranked by how
    strongly your taste converges on them; each carries the 'because' that earned it."""
    tracked = db.get_all_series(path)
    tracked_ids = {s["locg_series_id"] for s in tracked if s.get("locg_series_id")}
    my_pubs = {_pub_key(s["publisher"]) for s in tracked if s.get("publisher")}

    cand = {}
    for c in taste_profile(path, limit=top_creators):
        works = db.get_creator_works_cache(c["people_id"], path)
        if not works:
            continue
        for w in works:
            sid = w["locg_series_id"]
            if sid in tracked_ids:                       # already yours
                continue
            if _pub_key(w["publisher"]) not in my_pubs:  # only publishers you read
                continue
            if _NOISE_RE.search(w["title"]):             # reprints/promos
                continue
            e = cand.setdefault(sid, {
                "locg_series_id": sid, "title": w["title"],
                "publisher": w["publisher"], "score": 0, "because": {},
            })
            e["score"] += c["series_count"]              # weight by how much you like them
            e["because"][c["name"]] = c["series"][0]     # an example of your books by them

    ranked = sorted(cand.values(), key=lambda e: (e["score"], len(e["because"])), reverse=True)
    return [
        {
            "locg_series_id": e["locg_series_id"], "title": e["title"],
            "publisher": e["publisher"], "score": e["score"],
            "because": [{"creator": k, "your_series": v} for k, v in e["because"].items()],
        }
        for e in ranked
    ][:limit]
