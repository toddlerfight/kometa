import os
import json
import logging
import threading
from datetime import date
from contextlib import asynccontextmanager

import requests as _requests

logger = logging.getLogger(__name__)

# Auth-free session for fetching CDN images (S3 rejects Basic auth headers)
_img_session = _requests.Session()
_img_session.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.komga_client import KomgaClient
from kometa.metron_client import MetronClient
from kometa.comicvine_client import ComicVineClient, BASE_URL as CV_BASE_URL
from kometa.getcomics_client import GetComicsClient
from kometa.locg_client import search_series_anon as _locg_search_anon
from kometa.scheduler import start_scheduler
import kometa.db as db
import kometa.downloader as downloader
import kometa.matcher as matcher
from kometa.sources import (
    komga as _komga, metron as _metron, comicvine as _comicvine,
    locg as _locg, comics_root as _comics_root,
)
from kometa.naming import (
    find_issue_file as _find_issue_file, normalize_url as _normalize_url, norm as _norm,
    _resolve_dir,
)
from kometa.sync import sync_one as _sync_one
from kometa.acquisition import (
    set_progress, clear_progress, get_progress,
    _komga_scan, _process_queue, _sweep_missing,
    _poll_usenet_jobs, _release_day_retry,
)

DB_PATH = db.DB_PATH


def _sync_all_job():
    for s in db.get_all_series(DB_PATH):
        _sync_one(s)
    _sweep_missing()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    # Recover items orphaned by a mid-flight container restart
    db.reset_stuck_queue_items(DB_PATH)
    start_scheduler(_sync_all_job, _process_queue, _release_day_retry, _poll_usenet_jobs)
    yield


app = FastAPI(lifespan=lifespan)


# --- sync logic ---

def _summary(issues):
    from datetime import timedelta
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    owned = sum(1 for r in issues if r["owned"])
    missing = sum(1 for r in issues if not r["owned"] and (not r["store_date"] or r["store_date"] < today))
    upcoming = sum(1 for r in issues if not r["owned"] and r["store_date"] and r["store_date"] >= today)
    soon = [r["store_date"] for r in issues
            if not r["owned"] and r["store_date"] and today <= r["store_date"] <= cutoff]
    return {"owned": owned, "missing": missing, "upcoming": upcoming,
            "next_release": min(soon) if soon else None}


# --- connection tests ---

class TestKomgaRequest(BaseModel):
    url: str
    user: str
    password: str


@app.post("/api/test/komga")
def test_komga(req: TestKomgaRequest):
    try:
        client = KomgaClient(base_url=_normalize_url(req.url), auth=(req.user, req.password))
        r = client.session.get(f"{client.base_url}/api/v1/libraries", timeout=8)
        r.raise_for_status()
        raw = r.json()
        libs = raw if isinstance(raw, list) else raw.get("content", [])
        return {"ok": True, "libraries": [{"id": l["id"], "name": l["name"]} for l in libs]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestMetronRequest(BaseModel):
    user: str
    password: str


@app.post("/api/test/metron")
def test_metron(req: TestMetronRequest):
    try:
        client = MetronClient(auth=(req.user, req.password))
        r = client.session.get(f"{client.base_url}/series/", params={"name": "batman", "page": 1}, timeout=10)
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TestCVRequest(BaseModel):
    api_key: str


@app.post("/api/test/comicvine")
def test_comicvine(req: TestCVRequest):
    try:
        client = ComicVineClient(req.api_key)
        r = client.session.get(
            f"{CV_BASE_URL}/search/",
            params=client._params({"resources": "volume", "query": "batman", "limit": 1, "field_list": "id,name"}),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status_code") != 1:
            return {"ok": False, "error": data.get("error", "Unknown error")}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- config ---

@app.get("/api/config")
def get_config():
    import json
    cfg = db.get_config(DB_PATH)
    indexers_raw = cfg.get("newznab_indexers", "[]")
    try:
        indexers = json.loads(indexers_raw)
    except Exception:
        indexers = []
    # Strip apikeys from indexer list before returning
    safe_indexers = [{"name": i["name"], "host": i["host"], "ssl": i.get("ssl", True)} for i in indexers]
    root = _comics_root()
    return {
        "comics_root":         root,
        # Usable = exists and writable. Drives the just-in-time folder prompt:
        # a properly-mounted deploy is ok and never gets nagged.
        "comics_root_ok":      os.path.isdir(root) and os.access(root, os.W_OK),
        "komga_url":           cfg.get("komga_url", ""),
        "komga_user":          cfg.get("komga_user", ""),
        "komga_pass":          "",
        "komga_library_id":    cfg.get("komga_library_id", ""),
        "metron_user":         cfg.get("metron_user", ""),
        "metron_pass":         "",
        "cv_api_key":          "",
        "cv_configured":       bool(cfg.get("cv_api_key", "")),
        "locg_user":           cfg.get("locg_user", ""),
        "locg_pass":           "",
        "locg_configured":     bool(cfg.get("locg_user", "") and cfg.get("locg_pass", "")),
        "sync_hours":          cfg.get("sync_hours", "5,12,17"),
        "sab_url":             cfg.get("sab_url", ""),
        "sab_configured":      bool(cfg.get("sab_url", "") and cfg.get("sab_apikey", "")),
        "newznab_indexers":    safe_indexers,
    }


class ConfigRequest(BaseModel):
    comics_root:        str | None = None
    komga_url:          str | None = None
    komga_user:         str | None = None
    komga_pass:         str | None = None
    komga_library_id:   str | None = None
    metron_user:        str | None = None
    metron_pass:        str | None = None
    cv_api_key:         str | None = None
    locg_user:          str | None = None
    locg_pass:          str | None = None
    sync_hours:         str | None = None
    sab_url:            str | None = None
    sab_apikey:         str | None = None
    newznab_indexers:   str | None = None  # JSON array of {name, host, apikey, ssl}


@app.patch("/api/config")
def update_config(req: ConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None and v != ""}
    if "komga_url" in updates:
        updates["komga_url"] = _normalize_url(updates["komga_url"])
    db.set_config(updates, DB_PATH)
    return get_config()


def _load_indexers() -> list[dict]:
    import json
    try:
        return json.loads(db.get_config(DB_PATH).get("newznab_indexers", "[]"))
    except Exception:
        return []


def _save_indexers(indexers: list[dict]):
    import json
    db.set_config({"newznab_indexers": json.dumps(indexers)}, DB_PATH)


class IndexerRequest(BaseModel):
    name: str
    host: str
    apikey: str
    ssl: bool = True


@app.post("/api/config/indexers", status_code=201)
def add_indexer(req: IndexerRequest):
    # Add/remove operate on individual entries (not a whole-list re-save) so the
    # stored apikeys are never round-tripped through the browser and blanked.
    indexers = _load_indexers()
    indexers.append({"name": req.name, "host": req.host, "apikey": req.apikey, "ssl": req.ssl})
    _save_indexers(indexers)
    return {"ok": True, "count": len(indexers)}


@app.delete("/api/config/indexers/{idx}", status_code=204)
def remove_indexer(idx: int):
    indexers = _load_indexers()
    if not (0 <= idx < len(indexers)):
        raise HTTPException(404)
    indexers.pop(idx)
    _save_indexers(indexers)


# --- series routes ---

@app.get("/api/series")
def list_series():
    series = db.get_all_series(DB_PATH)
    summaries = db.get_all_series_summaries(DB_PATH)
    empty = {"owned": 0, "missing": 0, "upcoming": 0, "next_release": None}
    return [dict(s, **summaries.get(s["id"], empty)) for s in series]


@app.get("/api/series/{series_id}")
def get_series(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    return dict(s, issues=issues, **_summary(issues))


class AddSeriesRequest(BaseModel):
    metron_id: int | None = None
    locg_id: int | None = None
    folder_path: str | None = None
    komga_id: str | None = None
    on_pull_list: bool = True
    # Metadata from LOCG when metron_id is absent
    title: str | None = None
    publisher_name: str | None = None
    year_began: int | None = None


@app.post("/api/series", status_code=201)
def add_series(req: AddSeriesRequest):
    metron = _metron()

    title = req.title or ""
    publisher = req.publisher_name
    year_began = req.year_began
    metron_series_id = req.metron_id
    locg_series_id = req.locg_id
    folder_path = req.folder_path
    komga_series_id = req.komga_id

    if req.metron_id:
        # Metron-sourced: fetch canonical metadata
        ms = metron.get_series(req.metron_id)
        title = ms.get("name") or ms.get("series_name") or title
        pub = ms.get("publisher")
        publisher = pub.get("name") if isinstance(pub, dict) else (pub or publisher)
        year_began = ms.get("year_began") or year_began
    else:
        # LOCG-sourced: try to auto-link to Metron by title — but only if Metron
        # is configured. Without creds this is a guaranteed 401 we'd just swallow.
        cfg = db.get_config(DB_PATH)
        if cfg.get("metron_user") and cfg.get("metron_pass"):
            try:
                candidates = metron.search_series(title)
                match = next(
                    (r for r in candidates
                     if _norm(r.get("series") or r.get("name") or "") == _norm(title)),
                    None
                )
                if match:
                    metron_series_id = match["id"]
                    pub = match.get("publisher")
                    publisher = pub.get("name") if isinstance(pub, dict) else (pub or publisher)
                    year_began = match.get("year_began") or year_began
            except Exception:
                pass

    komga = _komga()
    if komga_series_id and komga:
        try:
            ks = komga.get_series(komga_series_id)
            title = ks.get("name") or title
            publisher = ks.get("metadata", {}).get("publisher") or publisher
            if not folder_path:
                folder_path = ks.get("url")
        except Exception:
            pass
    elif not komga_series_id and komga:
        # Try to auto-link to Komga by exact title match
        try:
            results = komga.search_series(title)
            match = next((r for r in results if (r.get("name") or "").lower() == title.lower()), None)
            if match:
                komga_series_id = match["id"]
        except Exception:
            pass

    # No folder yet (no Komga, or Komga had none)? Derive it from publisher+title.
    # _resolve_dir finds an existing on-disk folder (variation-tolerant) or returns
    # the canonical new path, so the first sync reconciles owned-vs-missing correctly
    # whether or not the series is already on disk. This is what makes Komga optional.
    if not folder_path:
        folder_path = _resolve_dir(_comics_root(), publisher or "Unknown", title)

    new_id = db.add_series(
        komga_series_id, metron_series_id,
        title=title,
        publisher=publisher,
        year_began=year_began,
        folder_path=folder_path,
        on_pull_list=req.on_pull_list,
        locg_series_id=locg_series_id,
        path=DB_PATH,
    )
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg_sync():
        _sync_one(added)
        if req.on_pull_list:
            issues = db.get_issues_for_series(new_id, DB_PATH)
            today_str = str(date.today())
            for issue in issues:
                if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today_str):
                    db.queue_issue(new_id, issue["number"], DB_PATH)
            _process_queue()

    threading.Thread(target=_bg_sync, daemon=True).start()
    return added


@app.delete("/api/series/{series_id}", status_code=204)
def delete_series(series_id: int):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    db.remove_series(series_id, DB_PATH)


class PullListRequest(BaseModel):
    on_pull_list: bool


@app.patch("/api/series/{series_id}/pull-list", status_code=200)
def toggle_pull_list(series_id: int, req: PullListRequest):
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    db.set_pull_list(series_id, req.on_pull_list, DB_PATH)
    return db.get_series_by_id(series_id, DB_PATH)


# --- library browse ---

@app.get("/api/library/komga")
def browse_komga(page: int = 0, size: int = 48, search: str = ""):
    komga = _komga()
    if not komga:
        raise HTTPException(503, "Komga not configured")
    params = {"page": page, "size": size, "sort": "metadata.titleSort,asc"}
    if search:
        params["search"] = search
    data = komga._get("/api/v1/series", params=params)

    tracked_map = {s["komga_series_id"]: s["id"] for s in db.get_all_series(DB_PATH)}

    items = [
        {
            "id": s["id"],
            "name": s["name"],
            "publisher": s.get("metadata", {}).get("publisher"),
            "year": s.get("metadata", {}).get("startYear"),
            "tracked": s["id"] in tracked_map,
            "tracked_id": tracked_map.get(s["id"]),
        }
        for s in data["content"]
    ]

    return {
        "items": items,
        "total": data["totalElements"],
        "page": data["number"],
        "pages": data["totalPages"],
        "last": data["last"],
    }


# --- search ---

@app.get("/api/search/komga")
def search_komga(q: str):
    komga = _komga()
    if not komga:
        raise HTTPException(503, "Komga not configured")
    return komga.search_series(q)


_STOP_WORDS = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "is", "it",
               "as", "by", "be", "or", "and", "but", "from", "with", "this", "that",
               "not", "are", "was", "were", "has", "have", "had", "its", "here", "there"}

def _metron_search_ranked(metron, q: str) -> list[dict]:
    results = metron.search_series(q)
    if results:
        return [dict(r, source="metron") for r in results]
    # Metron is punctuation-sensitive ("whats" ≠ "What's").
    # Retry with word pairs then single words, filtering to results that
    # contain every meaningful query word in their normalized name.
    words = [w for w in _norm(q).split() if len(w) > 2 and w not in _STOP_WORDS]
    if words:
        def _relevant(hits):
            return [r for r in hits if all(
                w in _norm(r.get('series') or r.get('name') or '')
                for w in words
            )]
        if len(words) >= 2:
            for i in range(len(words) - 1):
                good = _relevant(metron.search_series(f"{words[i]} {words[i+1]}"))
                if good:
                    return [dict(r, source="metron") for r in good]
        for word in sorted(words, key=len, reverse=True):
            good = _relevant(metron.search_series(word))
            if good:
                return [dict(r, source="metron") for r in good]
    return []


@app.get("/api/search/metron")
def search_metron(q: str):
    # Metron is optional. Not configured (no creds) or a transient failure both
    # return [] rather than 500, so the wizard falls through to LOCG cleanly
    # instead of dying on the Metron call. This is what makes key-free onboarding work.
    cfg = db.get_config(DB_PATH)
    if not (cfg.get("metron_user") and cfg.get("metron_pass")):
        return []
    try:
        return _metron_search_ranked(_metron(), q)
    except Exception as e:
        logger.warning(f"Metron search failed for {q!r}: {e}")
        return []


@app.get("/api/search/locg")
def search_locg(q: str):
    locg_client = _locg()
    raw = locg_client.search_series(q) if locg_client else _locg_search_anon(q)
    return [{
        "id":         r["id"],
        "series":     r["title"],
        "publisher":  {"name": r["publisher"]} if r["publisher"] else None,
        "year_began": r["year"],
        "cover":      r.get("cover"),
        "source":     "locg",
    } for r in raw[:15]]


# --- sync ---

@app.post("/api/sync")
def sync_all():
    threading.Thread(target=_sync_all_job, daemon=False).start()
    return {"ok": True, "started": True}


@app.post("/api/sync/{series_id}")
def sync_one(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    threading.Thread(target=_sync_one, args=(s,), daemon=True).start()
    return {"ok": True}


# --- pull list ---

@app.get("/api/pull-list")
def pull_list(days: int = 90, past: int = 0):
    return db.get_upcoming_issues(days, past, DB_PATH)


# --- thumbnails ---

@app.get("/api/series/{series_id}/thumbnail")
def series_thumbnail(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    komga = _komga()
    if s.get("komga_series_id") and komga:
        try:
            r = komga.session.get(
                f"{komga.base_url}/api/v1/series/{s['komga_series_id']}/thumbnail",
                timeout=5,
            )
            if r.ok:
                return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
        except Exception:
            pass
    # Use cached issue image URLs from DB — avoids live Metron API calls under concurrent grid load
    issues = db.get_issues_for_series(series_id, DB_PATH)
    img_url = next(
        (i["metron_image"] for i in sorted(issues, key=lambda x: x["number"])
         if i.get("metron_image")),
        None
    )
    if img_url:
        try:
            r2 = _img_session.get(img_url, timeout=8)
            if r2.ok:
                return Response(content=r2.content, media_type=r2.headers.get("content-type", "image/jpeg"))
        except Exception:
            pass
    raise HTTPException(404)


@app.get("/api/series/{series_id}/issues/{number}/thumbnail")
def issue_thumbnail(series_id: int, number: float):
    from fastapi.responses import RedirectResponse
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)

    book_id = issue.get("komga_book_id") if issue else None

    # Stale cache — live-lookup from Komga and write back so future calls are instant
    if not book_id:
        series = db.get_series_by_id(series_id, DB_PATH)
        komga_series_id = series.get("komga_series_id") if series else None
        if komga_series_id:
            komga = _komga()
            try:
                for b in komga.get_books(komga_series_id):
                    n = b["metadata"].get("numberSort")
                    if n is not None and float(n) == number:
                        book_id = b["id"]
                        db.upsert_issue_status(series_id, number, None, True, book_id, path=DB_PATH)
                        break
            except Exception:
                pass

    if book_id:
        komga = _komga()
        try:
            r = komga.session.get(
                f"{komga.base_url}/api/v1/books/{book_id}/thumbnail",
                timeout=5,
            )
            if r.ok:
                return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
        except Exception:
            pass

    if issue and issue.get("metron_image"):
        return RedirectResponse(issue["metron_image"])
    raise HTTPException(404)


@app.get("/api/book/{book_id}/thumbnail")
def book_thumbnail(book_id: str):
    komga = _komga()
    try:
        r = komga.session.get(
            f"{komga.base_url}/api/v1/books/{book_id}/thumbnail",
            timeout=5,
        )
        if r.ok:
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
        raise HTTPException(r.status_code)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(504) from e


@app.get("/api/komga/series/{komga_series_id}/books")
def komga_series_books(komga_series_id: str):
    komga = _komga()
    books = komga.get_books(komga_series_id)
    return [
        {
            "id": b["id"],
            "number": b["metadata"].get("numberSort"),
            "number_display": b["metadata"].get("number") or b["metadata"].get("numberSort"),
        }
        for b in books
    ]


@app.get("/api/komga/series/{komga_series_id}/thumbnail")
def komga_series_thumbnail(komga_series_id: str):
    komga = _komga()
    if not komga:
        raise HTTPException(503, "Komga not configured")
    r = komga.session.get(f"{komga.base_url}/api/v1/series/{komga_series_id}/thumbnail")
    if not r.ok:
        raise HTTPException(r.status_code)
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


# --- metron thumbnails ---

@app.get("/api/metron/series/{metron_id}/thumbnail")
def metron_series_thumbnail(metron_id: int):
    metron = _metron()
    try:
        detail = metron.get_series(metron_id)
        img_url = detail.get("image")

        if not img_url:
            cv = _comicvine()
            if cv:
                img_url = cv.find_series_image(
                    detail.get("name", ""),
                    detail.get("year_began"),
                )

        if not img_url:
            raise HTTPException(404)

        r = _img_session.get(img_url, timeout=10)
        r.raise_for_status()
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404) from e


@app.get("/api/metron/series/{metron_id}/info")
def metron_series_info(metron_id: int):
    metron = _metron()
    try:
        detail = metron.get_series(metron_id)
        return {
            "id":          metron_id,
            "issue_count": detail.get("issue_count"),
            "volume":      detail.get("volume"),
            "series_type": (detail.get("series_type") or {}).get("name", "") if isinstance(detail.get("series_type"), dict) else detail.get("series_type", ""),
        }
    except Exception as e:
        raise HTTPException(404) from e


# --- match / scan ---

@app.post("/api/match/scan")
def start_scan():
    if not _komga():
        raise HTTPException(503, "Komga not configured")
    if matcher.get_state()["running"]:
        return {"ok": False, "message": "Scan already running"}
    started = matcher.start(_komga, _metron, DB_PATH, sync_callback=_sync_all_job, locg_factory=_locg)
    return {"ok": started, "state": matcher.get_state()}


@app.post("/api/match/retry-empty")
def retry_empty_scan():
    """Re-scan none-confidence candidates that got no API results (rate-limit victims)."""
    if not _komga():
        raise HTTPException(503, "Komga not configured")
    if matcher.get_state()["running"]:
        return {"ok": False, "message": "Scan already running"}
    started = matcher.start(_komga, _metron, DB_PATH, sync_callback=_sync_all_job,
                            retry_empty=True, locg_factory=_locg)
    return {"ok": started, "state": matcher.get_state()}


@app.post("/api/match/rescore")
def rescore_candidates():
    """Re-evaluate stored medium/low candidates with current thresholds. No API calls."""
    result = matcher.rescore_candidates(DB_PATH)
    if result["promoted"]:
        threading.Thread(target=_sync_all_job, daemon=True).start()
    return result


@app.get("/api/match/status")
def scan_status():
    state = matcher.get_state()
    summary = db.get_candidates_summary(DB_PATH)
    counts = {"high": 0, "medium": 0, "low": 0, "none": 0, "skipped": 0}
    for row in summary:
        conf = row["confidence"]
        if conf in counts:
            counts[conf] += row["cnt"]
    return {**state, "counts": counts}


@app.get("/api/match/candidates")
def get_candidates():
    rows = db.get_pending_candidates(DB_PATH)
    groups: dict[str, list] = {"high": [], "medium": [], "low": [], "none": []}
    for r in rows:
        conf = r.get("confidence", "none")
        if conf in groups:
            groups[conf].append(r)
    return groups


@app.get("/api/match/candidates/{komga_series_id}")
def get_candidate_detail(komga_series_id: str):
    row = db.get_candidate_detail(komga_series_id, DB_PATH)
    if not row:
        raise HTTPException(404)
    c = dict(row)
    c["candidates"] = json.loads(c["candidates_json"]) if c.get("candidates_json") else []
    del c["candidates_json"]
    return c


class ConfirmRequest(BaseModel):
    komga_series_id: str
    metron_id: int


@app.post("/api/match/confirm")
def confirm_match(req: ConfirmRequest):
    existing = {s["komga_series_id"] for s in db.get_all_series(DB_PATH)}
    if req.komga_series_id not in existing:
        komga  = _komga()
        if not komga:
            raise HTTPException(503, "Komga not configured")
        metron = _metron()
        ks = komga.get_series(req.komga_series_id)
        ms = metron.get_series(req.metron_id)
        db.add_series(
            req.komga_series_id, req.metron_id,
            title       = ks["name"],
            publisher   = ks.get("metadata", {}).get("publisher"),
            year_began  = ms.get("year_began"),
            folder_path = ks.get("url"),
            path        = DB_PATH,
        )
        added = next(s for s in db.get_all_series(DB_PATH) if s["komga_series_id"] == req.komga_series_id)
        threading.Thread(target=_sync_one, args=(added,), daemon=True).start()
    db.confirm_candidate(req.komga_series_id, req.metron_id, DB_PATH)
    return {"ok": True}


class BulkItem(BaseModel):
    komga_series_id: str
    metron_id: int

class BulkConfirmRequest(BaseModel):
    items: list[BulkItem]


@app.post("/api/match/confirm-bulk")
def confirm_bulk(req: BulkConfirmRequest):
    komga = _komga()
    if not komga:
        raise HTTPException(503, "Komga not configured")
    metron = _metron()
    existing = {s["komga_series_id"] for s in db.get_all_series(DB_PATH)}
    confirmed, errors = 0, []

    for item in req.items:
        if item.komga_series_id in existing:
            db.confirm_candidate(item.komga_series_id, item.metron_id, DB_PATH)
            confirmed += 1
            continue
        try:
            ks = komga.get_series(item.komga_series_id)
            ms = metron.get_series(item.metron_id)
            db.add_series(
                item.komga_series_id, item.metron_id,
                title       = ks["name"],
                publisher   = ks.get("metadata", {}).get("publisher"),
                year_began  = ms.get("year_began"),
                folder_path = ks.get("url"),
                path        = DB_PATH,
            )
            db.confirm_candidate(item.komga_series_id, item.metron_id, DB_PATH)
            confirmed += 1
        except Exception as e:
            errors.append({"id": item.komga_series_id, "error": str(e)})

    def _bg_sync():
        for s in db.get_all_series(DB_PATH):
            try:
                _sync_one(s)
            except Exception as e:
                logger.warning(f"Background sync failed for series {s.get('id')} ({s.get('title')!r}): {e}")

    threading.Thread(target=_bg_sync, daemon=True).start()
    return {"confirmed": confirmed, "errors": errors}


class RejectRequest(BaseModel):
    komga_series_id: str


@app.post("/api/match/reject")
def reject_match(req: RejectRequest):
    db.reject_candidate(req.komga_series_id, DB_PATH)
    return {"ok": True}


# --- filesystem browse ---

def _fs_browse_default() -> str:
    """Where the filesystem picker should land — not the raw '/' full of OS guts.
    Prefer an existing comics root (the mount you're likely picking), else the
    home dir (where a desktop user keeps files), else '/'."""
    cr = os.path.realpath(_comics_root())
    if os.path.isdir(cr):
        return cr
    home = os.path.expanduser("~")
    return home if (home != "~" and home != "/" and os.path.isdir(home)) else "/"


@app.get("/api/fs/browse")
def browse_fs(path: str = "", scope: str = "library"):
    # 'library' stays sandboxed to the comics root (picking a series subfolder).
    # 'fs' browses the whole filesystem — needed to pick the comics root itself,
    # which by definition isn't inside the (maybe missing) comics root yet. It
    # still lands somewhere friendly (see _fs_browse_default), not bare '/'.
    if scope == "fs":
        root = "/"
        default = _fs_browse_default()
    else:
        root = default = os.path.realpath(_comics_root())
    target = os.path.realpath(path or default)
    if not target.startswith(root):
        raise HTTPException(403)
    if not os.path.isdir(target):
        raise HTTPException(404)
    try:
        names = sorted(
            n for n in os.listdir(target)
            if not n.startswith('.') and os.path.isdir(os.path.join(target, n))
        )
    except PermissionError:
        names = []
    parent = os.path.dirname(target)
    return {
        "path": target,
        "parent": parent if parent.startswith(root) and parent != target else None,
        "dirs": names,
    }


@app.get("/api/fs/resolve")
def resolve_folder(publisher: str = "", title: str = ""):
    """Preview where a series will be filed — the same publisher+title resolution
    add_series uses. Reports whether that folder already exists on disk so the UI
    can say 'existing' vs 'new'."""
    path = _resolve_dir(_comics_root(), publisher or "Unknown", title)
    return {"path": path, "exists": os.path.isdir(path)}


# --- folder path ---

class FolderRequest(BaseModel):
    folder_path: str | None = None


@app.patch("/api/series/{series_id}/folder")
def set_folder(series_id: int, req: FolderRequest):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    db.set_folder_path(series_id, req.folder_path or None, DB_PATH)
    return db.get_series_by_id(series_id, DB_PATH)



# --- download queue ---

@app.get("/api/queue")
def get_queue():
    items = db.get_queue(DB_PATH)
    for item in items:
        prog = get_progress(item["id"])
        if prog:
            item["progress"] = prog
    return items


@app.delete("/api/queue/{queue_id}", status_code=204)
def delete_queue_item(queue_id: int):
    db.remove_queue_item(queue_id, DB_PATH)


@app.post("/api/queue/{queue_id}/retry", status_code=200)
def retry_queue_item(queue_id: int):
    db.update_queue_state(queue_id, "queued", error=None, path=DB_PATH)
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


@app.post("/api/series/{series_id}/search-missing")
def search_missing(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    today = str(date.today())
    queued = 0
    for issue in issues:
        if not issue["owned"] and (not issue["store_date"] or issue["store_date"] <= today):
            db.queue_issue(series_id, issue["number"], DB_PATH)
            queued += 1
    if queued:
        threading.Thread(target=_process_queue, daemon=True).start()
    return {"queued": queued}


@app.post("/api/series/{series_id}/issues/{number}/search")
def search_issue(series_id: int, number: float):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    db.queue_issue(series_id, number, DB_PATH)
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


class DownloadFromUrlRequest(BaseModel):
    page_url: str


@app.post("/api/series/{series_id}/issues/{number}/download-from")
def download_from_url(series_id: int, number: float, req: DownloadFromUrlRequest):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue_row = next((i for i in issues if i["number"] == number), None)
    store_date = issue_row["store_date"] if issue_row else None

    gc = GetComicsClient()
    dl_url, hint_filename = gc._extract_download(req.page_url)
    if not dl_url:
        raise HTTPException(422, detail="No download link found on that page")

    db.queue_issue(series_id, number, DB_PATH)
    queue = db.get_queue(DB_PATH)
    item = next((q for q in queue if q["tracked_series_id"] == series_id and q["issue_number"] == number), None)
    if not item:
        raise HTTPException(500, detail="Queue item not found after insert")
    qid = item["id"]
    db.update_queue_state(qid, "downloading", source_url=dl_url, path=DB_PATH)

    def _do_download():
        try:
            dest = downloader.download_issue(
                url=dl_url,
                title=s["title"],
                publisher=s.get("publisher"),
                issue_number=number,
                store_date=store_date,
                hint_filename=hint_filename,
                komga_scan_fn=_komga_scan,
                progress_fn=lambda done, total: set_progress(qid, done, total),
                dest_dir=s.get("folder_path") or None,
                tracked_series_id=series_id,
                db_path=DB_PATH,
            )
            clear_progress(qid)
            db.update_queue_state(qid, "done", filename=dest, path=DB_PATH)
            if not s.get("folder_path"):
                db.set_folder_path(series_id, os.path.dirname(dest), DB_PATH)
            db.upsert_issue_status(series_id, number, store_date, owned=True, path=DB_PATH)
        except Exception as e:
            clear_progress(qid)
            db.update_queue_state(qid, "failed", error=str(e), path=DB_PATH)

    threading.Thread(target=_do_download, daemon=True).start()
    return {"ok": True, "download_url": dl_url}


@app.post("/api/queue/sweep")
def manual_sweep():
    def _sweep_and_process():
        _sweep_missing()
        _process_queue()
    threading.Thread(target=_sweep_and_process, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/process")
def manual_process():
    threading.Thread(target=_process_queue, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/clear-history", status_code=204)
def clear_queue_history():
    db.clear_queue_history(DB_PATH)


@app.get("/api/series/{series_id}/issues/{number}/metron")
def get_issue_metron(series_id: int, number: float):
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue or not issue.get("metron_issue_id"):
        raise HTTPException(404)
    try:
        detail = _metron().get_issue(issue["metron_issue_id"])
        return {
            "desc":       detail.get("desc"),
            "title":      detail.get("name") or detail.get("title", ""),
            "credits":    detail.get("credits", []),
            "characters": [c.get("name", "") for c in detail.get("characters", [])],
            "arcs":       [a.get("name", "") for a in detail.get("arcs", [])],
        }
    except Exception as e:
        raise HTTPException(404) from e


@app.get("/api/series/{series_id}/issues/{number}/queue-status")
def get_issue_queue_status(series_id: int, number: float):
    queue = db.get_queue(DB_PATH)
    q = next((q for q in queue if q["tracked_series_id"] == series_id and q["issue_number"] == number), None)
    if not q:
        return {"state": None}
    result = {"state": q["state"], "error": q.get("error")}
    prog = get_progress(q["id"])
    if prog:
        result["progress"] = prog
    return result


@app.get("/api/series/{series_id}/issues/{number}/variants")
def get_issue_variants(series_id: int, number: float):
    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue:
        raise HTTPException(404)
    locg_issue_id = issue.get("locg_issue_id")
    if not locg_issue_id:
        return {"covers": [], "locg_issue_id": None}
    try:
        locg = _locg()
        if locg:
            data = locg.fetch_variants(locg_issue_id)
        else:
            from kometa.locg_client import fetch_variants
            data = fetch_variants(locg_issue_id)
        return {"covers": data["covers"], "locg_issue_id": locg_issue_id}
    except Exception as e:
        raise HTTPException(502, detail=str(e)) from e


class VariantApplyRequest(BaseModel):
    selected: list
    primary_id: str


@app.post("/api/series/{series_id}/issues/{number}/variants/apply")
def apply_issue_variants(series_id: int, number: float, req: VariantApplyRequest):
    if not req.selected:
        raise HTTPException(400, detail="No variants selected")

    series = db.get_series_by_id(series_id, DB_PATH)
    if not series:
        raise HTTPException(404)

    issues = db.get_issues_for_series(series_id, DB_PATH)
    issue = next((i for i in issues if i["number"] == number), None)
    if not issue:
        raise HTTPException(404)

    if issue.get("owned"):
        file_path = _find_issue_file(series.get("folder_path", ""), series["title"], number)
        if not file_path:
            raise HTTPException(404, detail="File not found on disk")
        try:
            from kometa.downloader import inject_covers
            added = inject_covers(file_path, req.selected, req.primary_id)
            return {"ok": True, "added": added}
        except Exception as e:
            raise HTTPException(500, detail=str(e)) from e
    else:
        db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
        return {"ok": True, "queued": True}


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
