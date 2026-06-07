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
from kometa.locg_client import get_issue_details_anon

logger = logging.getLogger(__name__)

# LOCG rate-limits hard; throttle enrichment and back off on 429.
_ENRICH_DELAY = 1.5
_ENRICH_RETRIES = 3

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
