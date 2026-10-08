"""Issue details and variant covers — from whichever source has them.

The issue modal's Details and Variants tabs used to be LOCG-only, so when LOCG
shut its door (2026-10-08) both went dark. Metron's issue record carries the
same things — description, credits, every variant with an image — through an
API that wants to be read. Metron first; LOCG only for issues Metron hasn't
got. Either way the result is cached, so a modal reopen costs nobody a request.
"""
import logging

import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH
METRON_CACHE_DAYS = 7      # variants keep landing right up to release day
MISSING = {"desc": "", "credits": [], "covers": []}


def _metron(issue: dict, path) -> dict | None:
    from kometa import metron_client
    mid = issue.get("metron_issue_id")
    if not mid or not metron_client.configured():
        return None
    key = f"metron:{mid}"
    cached = db.get_issue_details_cache(key, path, max_age_days=METRON_CACHE_DAYS)
    if cached is not None:
        return cached
    try:
        detail = metron_client.issue_detail(mid)
    except metron_client.MetronUnavailable as e:
        logger.info(f"Metron details unavailable for issue {mid}: {e}")
        stale = db.get_issue_details_cache(key, path)       # old beats nothing
        return stale
    db.set_issue_details_cache(key, detail, path)
    return detail


def _locg(issue: dict, path, want_covers: bool) -> dict | None:
    lid = issue.get("locg_issue_id")
    if not lid:
        return None
    from kometa import locg_client
    cached = db.get_issue_details_cache(lid, path)
    if cached is None:
        cached = locg_client.get_issue_details_anon(lid)     # raises when LOCG is shut
        db.set_issue_details_cache(lid, cached, path)
    out = {**cached, "source": "locg"}
    if want_covers:
        out["covers"] = locg_client.fetch_variants(lid)["covers"]
    return out


def details_for(issue: dict, path=None, want_covers: bool = False) -> dict:
    """{desc, credits, covers?, source}. Raises only when the ONLY source that
    could answer didn't (that's a 502, not 'no details')."""
    path = path or DB_PATH
    found = _metron(issue, path)
    if found is not None and (not want_covers or found.get("covers")):
        return {**found, "source": "metron"}
    try:
        locg = _locg(issue, path, want_covers)
    except Exception:
        if found is not None:
            return {**found, "source": "metron"}
        raise
    if locg is not None:
        return locg
    return {**MISSING, **(found or {}), "source": "metron" if found else None}
