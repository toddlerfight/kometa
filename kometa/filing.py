"""Collected editions filed as series → under the run they collect (the pass
the single-file report feeds). Dry run first; each move is your click.

For every single-file series the report calls 'collected', ask the publisher
wiki what the book collects (kometa/fandom_client.py), take the run with the
most collected issues as the parent, and look for that run on your shelf.
  ready      — parent found on the shelf: one click files it
  no_parent  — we know the parent; you don't have that run
  not_found  — the wiki has no page for it (or the publisher has no wiki)
Lookups run in the background at the wiki's pace and are cached a month in
issue_details_cache (key 'fandom:<wiki>:<norm title>').
"""
import logging
import re
import threading
from collections import Counter

import kometa.db as db
from kometa import fandom_client as fc
from kometa.naming import norm_key

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH
CACHE_DAYS = 30
_job = threading.Lock()
_progress = {"running": False, "done": 0, "total": 0}


def _cache_key(wiki: str, title: str) -> str:
    return f"fandom:{wiki}:{norm_key(re.sub(r'\(\d{4}\)', '', title))}"


def _title_keys(title: str) -> set[str]:
    """The shapes a shelf title can take for one run: 'Batman: Year 100',
    'Batman - Year 100', 'Batman - Year 100 (2006)'."""
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
    return {norm_key(t), norm_key(re.sub(r"\s*:\s*", " - ", t)), norm_key(re.sub(r"\s+Vol\s+\d+$", "", t, flags=re.I))}


def choose_parent(collects: list[dict]) -> tuple[str | None, int, int]:
    """(series name, issues of it collected, issues collected in all)."""
    if not collects:
        return None, 0, 0
    c = Counter(e["series"] for e in collects)
    name, n = c.most_common(1)[0]
    return name, n, len(collects)


def _shelf_index(series: list[dict]) -> dict[str, dict]:
    idx = {}
    for s in series:
        if s.get("kind") == "arc" or not s.get("folder_path"):
            continue
        if s.get("match_status") in ("pending", "needs_match"):
            continue
        for k in _title_keys(s["title"]):
            idx.setdefault(k, s)
    return idx


def lookup(series: dict, path=None) -> dict | None:
    """Cached wiki lookup for one series. None = not found / no wiki."""
    path = path or DB_PATH
    wiki = fc.wiki_for(series.get("publisher"))
    if not wiki:
        return None
    key = _cache_key(wiki, series["title"])
    hit = db.get_issue_details_cache(key, path, max_age_days=CACHE_DAYS)
    if hit is not None:
        return hit or None                      # {} cached = looked, nothing there
    found = fc.lookup_collection(series["title"], series.get("publisher"))
    db.set_issue_details_cache(key, found or {}, path)
    return found


def row_for(series: dict, found: dict | None, shelf: dict[str, dict]) -> dict:
    out = {"id": series["id"], "title": series["title"], "publisher": series.get("publisher")}
    if not found:
        return {**out, "status": "not_found", "why": "no wiki for this publisher" if not fc.wiki_for(series.get("publisher"))
                else "no collected-edition page found on the wiki"}
    parent_name, n, total = choose_parent(found["collects"])
    parent = next((shelf[k] for k in _title_keys(parent_name) if k in shelf), None) if parent_name else None
    nums = sorted(e["number"] for e in found["collects"] if e["series"] == parent_name)
    span = (f"#{nums[0]:g}–{nums[-1]:g}" if len(nums) > 1 else f"#{nums[0]:g}") if nums else ""
    base = {**out, "year": found.get("year"), "isbn": found.get("isbn"), "wiki_url": found.get("url"), "wiki_page": found.get("page"),
            "parent_name": parent_name, "collects_summary": f"{parent_name} {span}" + (f" + {total - n} more" if total > n else ""),
            "collects": found["collects"]}
    if parent and parent["id"] != series["id"]:
        return {**base, "status": "ready", "parent_id": parent["id"], "parent_title": parent["title"]}
    return {**base, "status": "no_parent", "why": f"you don't have {parent_name} on the shelf"}


def plan(path=None, candidates: list[dict] | None = None, do_lookup=lookup) -> dict:
    """Rows for every 'collected' single-file series, from the cache only —
    nothing fetched here. Call refresh_in_background() to fill the cache."""
    from kometa import singles
    path = path or DB_PATH
    all_series = db.get_all_series(path)
    shelf = _shelf_index(all_series)
    if candidates is None:
        rep = singles.report(path)
        ids = {r["id"] for r in rep["rows"] if r["kind"] == "collected"}
        candidates = [s for s in all_series if s["id"] in ids]
    rows = []
    for s in candidates:
        wiki = fc.wiki_for(s.get("publisher"))
        cached = db.get_issue_details_cache(_cache_key(wiki, s["title"]), path, max_age_days=CACHE_DAYS) if wiki else None
        if wiki and cached is None and do_lookup is lookup:
            rows.append({"id": s["id"], "title": s["title"], "publisher": s.get("publisher"), "status": "pending", "why": "wiki lookup queued"})
            continue
        found = (cached or None) if do_lookup is lookup else do_lookup(s, path)
        rows.append(row_for(s, found, shelf))
    counts = Counter(r["status"] for r in rows)
    return {"rows": rows, "counts": dict(counts), "progress": dict(_progress)}


def refresh(path=None) -> int:
    """Fill the cache for every collected single-file series without one."""
    from kometa import singles
    path = path or DB_PATH
    if not _job.acquire(blocking=False):
        return 0
    try:
        rep = singles.report(path)
        ids = {r["id"] for r in rep["rows"] if r["kind"] == "collected"}
        todo = []
        for s in db.get_all_series(path):
            if s["id"] not in ids:
                continue
            wiki = fc.wiki_for(s.get("publisher"))
            if wiki and db.get_issue_details_cache(_cache_key(wiki, s["title"]), path, max_age_days=CACHE_DAYS) is None:
                todo.append(s)
        _progress.update(running=True, done=0, total=len(todo))
        n = 0
        for s in todo:
            try:
                lookup(s, path)
                n += 1
            except fc.FandomUnavailable as e:
                logger.info(f"Wiki lookup paused: {e}")
                break
            finally:
                _progress["done"] += 1
        return n
    finally:
        _progress["running"] = False
        _job.release()


def refresh_in_background():
    if _progress["running"]:
        return
    threading.Thread(target=refresh, name="collections-lookup", daemon=True).start()


def apply(series_id: int, parent_id: int, path=None) -> dict:
    """File it. The year comes from the wiki when we have it."""
    from kometa import tidy
    path = path or DB_PATH
    s = db.get_series_by_id(series_id, path)
    if not s:
        raise tidy.TidyError("No such series")
    found = None
    wiki = fc.wiki_for(s.get("publisher"))
    if wiki:
        found = db.get_issue_details_cache(_cache_key(wiki, s["title"]), path) or None
    return tidy.file_under(series_id, parent_id, path, year=(found or {}).get("year"))
