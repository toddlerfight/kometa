"""'Get' on a reading list: an entry you don't have becomes an acquisition.

The shape we settled on (2026-10-09): track the run WITHOUT putting it on the
pull list, and request only the issues the list entry covers. A TPB-order entry
('B.P.R.D.: Plague of Frogs #1', the series once in the list) covers its whole
run; a volume-by-volume entry ('Witchfinder #3') covers that one book. Issues
the list did not ask for are marked ignored on a series this created, so the
Library count stays honest (1 of 1, not 1 of 5) — un-ignore from the series
page if you decide you want the whole run.

Runs come from Metron then LOCG, by the matcher's own confident rule. A run no
catalogue knows is reported as such; the name-only trade grab is a separate
build (it needs the confirm-before-place step).
"""
import logging
import os
import re
import threading
from datetime import date

from fastapi import APIRouter, HTTPException

import kometa.db as db
from kometa import readlists, sources
from kometa.naming import norm_key, _resolve_dir

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
_YEAR4 = re.compile(r"^(19|20)\d{2}$")


class ListGetError(RuntimeError):
    pass


def _find_run(entry: dict, item: dict, search_metron=None, search_locg=None) -> dict | None:
    """{source, id, title, publisher, year} for the run a list entry names, or None."""
    from kometa.shelf_import import find_metron_match, find_confident_match
    title = entry["series"]
    vol = item.get("volume") or ""
    if _YEAR4.match(vol) and not re.search(r"\(\d{4}\)\s*$", title):
        title = f"{title} ({vol})"                   # the CBL's volume year disambiguates same-named runs
    fake = {"title": title, "publisher": None, "folder_path": None}
    try:
        mid = find_metron_match(fake, search=search_metron) if (search_metron or _metron_on()) else None
    except Exception as e:
        logger.info(f"List get: Metron didn't answer for {title!r}: {e}")
        mid = None
    if mid:
        m = fake["_matched"]
        return {"source": "metron", "id": mid, "title": m.get("title") or entry["series"],
                "publisher": m.get("publisher"), "year": m.get("year")}
    try:
        lid = find_confident_match(fake, search=search_locg) if search_locg else find_confident_match(fake)
    except Exception as e:
        logger.info(f"List get: LOCG didn't answer for {title!r}: {e}")
        lid = None
    if lid:
        m = fake["_matched"]
        return {"source": "locg", "id": lid, "title": m.get("title") or entry["series"],
                "publisher": m.get("publisher"), "year": m.get("year")}
    return None


def _metron_on() -> bool:
    from kometa import metron_client
    return metron_client.configured()


def _existing_for(run: dict, path) -> dict | None:
    col = "metron_series_id" if run["source"] == "metron" else "locg_series_id"
    for s in db.get_all_series(path):
        if s.get("kind") != "arc" and s.get(col) == run["id"]:
            return s
    return None


def _track(run: dict, list_id: int, path, root: str) -> dict:
    """A new tracked series for the run, pull list OFF, tagged with the list."""
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", run["title"]).strip()
    folder = _resolve_dir(root, run.get("publisher") or "Unknown", title)
    sid = db.add_series(title=title, publisher=run.get("publisher"), year_began=run.get("year"),
                        folder_path=folder, on_pull_list=False,
                        locg_series_id=run["id"] if run["source"] == "locg" else None, path=path)
    if run["source"] == "metron":
        db.set_metron_series_id(sid, run["id"], path)
        db.set_metron_link(sid, "linked", path)
    db.set_match_status(sid, "manual", path)
    db.set_from_list(sid, list_id, path)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger.warning(f"List get: couldn't create {folder}: {e}")
    return db.get_series_by_id(sid, path)


def get_entry(list_id: int, item_id: int, path=None, search_metron=None, search_locg=None, sync=None,
              root: str | None = None) -> dict:
    """One entry → tracked run (if needed) + its issues queued. Returns what happened."""
    path = path or DB_PATH
    root = root or sources.comics_root()
    res = readlists.resolve(list_id, path)
    entry = next((e for e in res["entries"] if e["item_id"] == item_id), None)
    if not entry:
        raise ListGetError("No such entry")
    if entry["status"] == "owned":
        return {"entry": entry["series"], "result": "owned"}
    item = next(it for it in readlists.get_items(list_id, path) if it["id"] == item_id)
    per_series = sum(1 for e in res["entries"] if norm_key(e["series"]) == norm_key(entry["series"]))
    whole_run = per_series == 1
    want_n = readlists._num(entry["number"]) if not whole_run else None

    series = db.get_series_by_id(entry["series_id"], path) if entry.get("series_id") else None
    created = False
    if not series:
        run = _find_run(entry, item, search_metron, search_locg)
        if not run:
            return {"entry": entry["series"], "result": "unknown",
                    "detail": "No catalogue knows this run (Metron, LOCG)"}
        series = _existing_for(run, path)
        if not series:
            series = _track(run, list_id, path, root)
            created = True
            (sync or _sync)(series)
            series = db.get_series_by_id(series["id"], path)
    issues = db.get_issues_for_series(series["id"], path)
    if not issues:
        return {"entry": entry["series"], "result": "no_issues", "series_id": series["id"],
                "detail": "Tracked, but the catalogue lists no issues yet"}
    today = str(date.today())
    wanted = [i for i in issues if whole_run or i["number"] == want_n]
    pairs = [(series["id"], i["number"]) for i in wanted
             if not i["owned"] and (not i["store_date"] or i["store_date"] <= today)]
    if pairs:
        db.queue_issues_bulk(pairs, path)
    ignored = 0
    if created and not whole_run:
        # the issues this list never asked for: not missing, just not wanted
        for i in issues:
            if i["number"] != want_n and not i["owned"]:
                db.set_issue_ignored(series["id"], i["number"], True, path)
                ignored += 1
    return {"entry": entry["series"], "result": "queued", "series_id": series["id"], "series_title": series["title"],
            "created": created, "queued": len(pairs), "ignored": ignored,
            "upcoming": sum(1 for i in wanted if not i["owned"] and i["store_date"] and i["store_date"] > today)}


def _sync(series: dict):
    from kometa.sync import sync_one, sync_one_guarded
    sync_one_guarded(series, lambda s: sync_one(s, force=True))


# --- the whole list, in the background --------------------------------------------
_jobs: dict[int, dict] = {}
_lock = threading.Lock()


def get_missing(list_id: int, path=None, **kw) -> dict:
    path = path or DB_PATH
    res = readlists.resolve(list_id, path)
    gaps = [e for e in res["entries"] if e["status"] != "owned"]
    out = {"total": len(gaps), "done": 0, "queued": 0, "created": 0, "unknown": [], "errors": [], "running": True}
    _jobs[list_id] = out
    for e in gaps:
        try:
            r = get_entry(list_id, e["item_id"], path, **kw)
            if r["result"] == "queued":
                out["queued"] += r["queued"]
                out["created"] += int(r["created"])
            elif r["result"] in ("unknown", "no_issues"):
                out["unknown"].append(e["series"])
        except Exception as ex:
            out["errors"].append({"entry": e["series"], "error": str(ex)})
        out["done"] += 1
    out["running"] = False
    try:
        from kometa.acquisition import _process_queue
        threading.Thread(target=_process_queue, daemon=True).start()
    except Exception:
        pass
    return out


def start_get_missing(list_id: int) -> dict:
    with _lock:
        job = _jobs.get(list_id)
        if job and job.get("running"):
            return job
        res = readlists.resolve(list_id)
        n = sum(1 for e in res["entries"] if e["status"] != "owned")
        _jobs[list_id] = {"total": n, "done": 0, "queued": 0, "created": 0, "unknown": [], "errors": [], "running": True}
    threading.Thread(target=get_missing, args=(list_id,), daemon=True).start()
    return _jobs[list_id]


# --- API --------------------------------------------------------------------------
@router.post("/api/readlists/{list_id}/items/{item_id}/get")
def api_get_entry(list_id: int, item_id: int):
    try:
        r = get_entry(list_id, item_id)
    except ListGetError as e:
        raise HTTPException(404, str(e))
    if r["result"] == "queued" and r["queued"]:
        from kometa.acquisition import _process_queue
        threading.Thread(target=_process_queue, daemon=True).start()
    return r


@router.post("/api/readlists/{list_id}/get-missing")
def api_get_missing(list_id: int):
    return start_get_missing(list_id)


@router.get("/api/readlists/{list_id}/get-missing")
def api_get_missing_status(list_id: int):
    return _jobs.get(list_id) or {"running": False, "total": 0, "done": 0}
