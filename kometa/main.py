import os
import re
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
from kometa.comicvine_client import ComicVineClient
from kometa.getcomics_client import GetComicsClient, GCRateLimitError
from kometa.locg_client import search_series_anon as _locg_search_anon
from kometa.scheduler import start_scheduler
import kometa.db as db
import kometa.downloader as downloader
import kometa.matcher as matcher

DB_PATH = os.environ.get("KOMETA_DB", "/data/kometa.db")
_dl_progress: dict[int, dict] = {}

_komga_instance: "KomgaClient | None" = None
_komga_cfg_key: str = ""
_metron_instance: "MetronClient | None" = None
_metron_cfg_key: str = ""


def _komga() -> KomgaClient | None:
    global _komga_instance, _komga_cfg_key
    cfg = db.get_config(DB_PATH)
    if not cfg.get("komga_url"):
        return None
    key = f"{cfg.get('komga_url')}|{cfg.get('komga_user')}|{cfg.get('komga_pass')}|{cfg.get('komga_library_id')}"
    if _komga_instance is None or key != _komga_cfg_key:
        _komga_instance = KomgaClient(
            base_url=cfg.get("komga_url", ""),
            auth=(cfg.get("komga_user", ""), cfg.get("komga_pass", "")),
            library_id=cfg.get("komga_library_id", ""),
        )
        _komga_cfg_key = key
    return _komga_instance


def _metron() -> MetronClient:
    global _metron_instance, _metron_cfg_key
    cfg = db.get_config(DB_PATH)
    key = f"{cfg.get('metron_user')}|{cfg.get('metron_pass')}"
    if _metron_instance is None or key != _metron_cfg_key:
        _metron_instance = MetronClient(auth=(cfg.get("metron_user", ""), cfg.get("metron_pass", "")))
        _metron_cfg_key = key
    return _metron_instance


def _comicvine() -> ComicVineClient | None:
    key = db.get_config(DB_PATH).get("cv_api_key", "")
    return ComicVineClient(key) if key else None


def _locg():
    cfg = db.get_config(DB_PATH)
    user = cfg.get("locg_user", "")
    pw   = cfg.get("locg_pass", "")
    if not user or not pw:
        return None
    try:
        from kometa.locg_client import LOCGClient
        client = LOCGClient(user, pw, session=cfg.get("locg_session") or None)
        # Persist refreshed session if it changed (re-login happened)
        if client.session_cookie and client.session_cookie != cfg.get("locg_session"):
            db.set_config({"locg_session": client.session_cookie}, DB_PATH)
        return client
    except Exception as e:
        logger.warning(f"LoCG init failed: {e}")
        return None


def _sync_all_job():
    for s in db.get_all_series(DB_PATH):
        _sync_one(s)


def _komga_scan():
    komga = _komga()
    if komga:
        komga.scan_library()


def _process_queue():
    items = db.get_queued_items(DB_PATH)
    if not items:
        return
    gc = GetComicsClient()
    downloaded_urls = set()
    for item in items:
        qid = item["id"]
        db.update_queue_state(qid, "searching", path=DB_PATH)
        try:
            issues = db.get_issues_for_series(item["tracked_series_id"], DB_PATH)
            issue_row = next((i for i in issues if i["number"] == item["issue_number"]), None)
            store_date = issue_row["store_date"] if issue_row else None

            dl_url, hint_filename = gc.search(item["title"], item["issue_number"], store_date)
            if not dl_url:
                db.update_queue_state(qid, "not_found", error="No result on GetComics", path=DB_PATH)
                continue

            if dl_url in downloaded_urls:
                db.update_queue_state(qid, "not_found", error="Pack already downloaded for this series", path=DB_PATH)
                continue
            downloaded_urls.add(dl_url)

            db.update_queue_state(qid, "downloading", source_url=dl_url, path=DB_PATH)
            dest = downloader.download_issue(
                url=dl_url,
                title=item["title"],
                publisher=item["publisher"],
                issue_number=item["issue_number"],
                store_date=store_date,
                hint_filename=hint_filename,
                komga_scan_fn=_komga_scan,
                progress_fn=lambda done, total: _dl_progress.update({qid: {"done": done, "total": total}}),
                dest_dir=item.get("folder_path") or None,
                tracked_series_id=item["tracked_series_id"],
                db_path=DB_PATH,
            )
            _dl_progress.pop(qid, None)
            db.update_queue_state(qid, "done", filename=dest, path=DB_PATH)
            # Auto-populate folder_path so the next sync's folder scan finds the file.
            if not item.get("folder_path"):
                db.set_folder_path(item["tracked_series_id"], os.path.dirname(dest), DB_PATH)
            # Mark owned immediately — we placed the file, we know it's there.
            # komga_book_id stays NULL until next full sync (only needed for thumbnails).
            db.upsert_issue_status(
                item["tracked_series_id"], item["issue_number"], store_date,
                in_komga=True, path=DB_PATH,
            )
        except GCRateLimitError:
            db.update_queue_state(qid, "failed", error="Rate limited by GetComics — wait a few minutes before retrying", path=DB_PATH)
            break  # stop processing the rest of the queue too, we're blocked
        except Exception as e:
            db.update_queue_state(qid, "failed", error=str(e), path=DB_PATH)


def _sweep_missing():
    """Queue all missing issues for monitored series."""
    rows = db.get_missing_for_monitored(DB_PATH)
    for row in rows:
        db.queue_issue(row["tracked_series_id"], row["number"], DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    # Recover items orphaned by a mid-flight container restart
    db.reset_stuck_queue_items(DB_PATH)
    start_scheduler(_sync_all_job, _process_queue, _sweep_missing)
    yield


app = FastAPI(lifespan=lifespan)


# --- sync logic ---

def _parse_issue_number(filename: str, series_title: str = "") -> float | None:
    import re
    name = os.path.splitext(filename)[0]
    # #001 or #1.5
    m = re.search(r'#(\d+(?:\.\d+)?)', name)
    if m:
        return float(m.group(1))
    # Issue 001
    m = re.search(r'\bIssue\s+(\d+(?:\.\d+)?)\b', name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # Strip series title then find first number under 1000 (avoids years)
    remainder = name
    if series_title:
        remainder = re.sub(re.escape(series_title), '', name, count=1, flags=re.IGNORECASE).strip(' -_')
    for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', remainder):
        val = float(m.group(1))
        if val < 1000:
            return val
    return None


def _scan_folder_numbers(folder_path: str, series_title: str = "") -> set[float]:
    exts = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
    numbers = set()
    try:
        for name in os.listdir(folder_path):
            if os.path.splitext(name)[1].lower() in exts:
                num = _parse_issue_number(name, series_title)
                if num is not None:
                    numbers.add(num)
    except Exception:
        pass
    return numbers


def _sync_one(series: dict):
    komga = _komga()
    metron = _metron()

    # Lazily populate folder_path from Komga on first sync (only if linked)
    if not series.get("folder_path") and series.get("komga_series_id") and komga:
        try:
            komga_series = komga.get_series(series["komga_series_id"])
            fp = komga_series.get("url")
            if fp:
                db.set_folder_path(series["id"], fp, DB_PATH)
                series = dict(series, folder_path=fp)
        except Exception:
            pass

    # Komga book map — used for ownership supplement and book IDs (thumbnails)
    book_map = {}
    if series.get("komga_series_id") and komga:
        try:
            for b in komga.get_books(series["komga_series_id"]):
                if b.get("media", {}).get("status") == "ERROR":
                    continue
                n = b["metadata"]["numberSort"]
                if n is not None:
                    book_map[float(n)] = b["id"]
        except Exception:
            pass

    # Ownership = what's on disk. book_map is only used to populate komga_book_id for thumbnails.
    folder = series.get("folder_path")
    if folder and os.path.isdir(folder):
        owned_numbers = _scan_folder_numbers(folder, series.get("title", ""))
    else:
        owned_numbers = set(book_map.keys())

    # --- Build issue map from Metron (primary) ---
    issue_map: dict[float, dict] = {}
    if series.get("metron_series_id"):
        for issue in metron.get_issues(series["metron_series_id"]):
            try:
                num = float(issue["number"])
            except (ValueError, TypeError):
                continue
            issue_map[num] = {"store_date": issue.get("store_date"), "image": issue.get("image"), "metron_issue_id": issue.get("id")}

    # --- Supplement from ComicVine ---
    cv = _comicvine()
    if cv:
        try:
            cv_vol_id = series.get("cv_volume_id")
            if not cv_vol_id and series.get("year_began"):
                # Require year_began for CV lookup — title-only matching is too ambiguous
                cv_vol_id = cv.get_volume_id(series["title"], series.get("year_began"))
                if cv_vol_id:
                    db.set_cv_volume_id(series["id"], cv_vol_id, DB_PATH)
                    series = dict(series, cv_volume_id=str(cv_vol_id))
            if cv_vol_id:
                for ci in cv.get_issues(int(cv_vol_id)):
                    num = ci["number"]
                    if num not in issue_map:
                        issue_map[num] = {"store_date": ci["store_date"], "image": ci["cover"]}
                    else:
                        if not issue_map[num]["store_date"]:
                            issue_map[num]["store_date"] = ci["store_date"]
                        if not issue_map[num]["image"]:
                            issue_map[num]["image"] = ci["cover"]
        except Exception as e:
            logger.warning(f"CV supplement failed for '{series['title']}': {e}")

    # --- Supplement from LoCG (best for upcoming solicitations) ---
    locg = _locg()
    if locg:
        try:
            locg_id = series.get("locg_series_id")
            if not locg_id:
                locg_id = locg.find_series_id(series["title"], series.get("year_began"))
                if locg_id:
                    db.set_locg_series_id(series["id"], locg_id, DB_PATH)
                    series = dict(series, locg_series_id=locg_id)
            if locg_id:
                for li in locg.get_issues(locg_id):
                    num = li["number"]
                    if num not in issue_map:
                        issue_map[num] = {"store_date": li["store_date"], "image": li["cover"], "locg_issue_id": li.get("locg_issue_id")}
                    else:
                        if not issue_map[num]["store_date"]:
                            issue_map[num]["store_date"] = li["store_date"]
                        if not issue_map[num]["image"]:
                            issue_map[num]["image"] = li["cover"]
                        if not issue_map[num].get("locg_issue_id"):
                            issue_map[num]["locg_issue_id"] = li.get("locg_issue_id")
        except Exception as e:
            logger.warning(f"LoCG supplement failed for '{series['title']}': {e}")

    # --- Upsert merged issue list ---
    for num, data in issue_map.items():
        db.upsert_issue_status(
            series["id"], num, data["store_date"],
            num in owned_numbers, book_map.get(num),
            metron_image=data.get("image"),
            metron_issue_id=data.get("metron_issue_id"),
            locg_issue_id=data.get("locg_issue_id"),
            path=DB_PATH,
        )
    db.mark_synced(series["id"], DB_PATH)


def _summary(issues):
    from datetime import timedelta
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    owned = sum(1 for r in issues if r["in_komga"])
    missing = sum(1 for r in issues if not r["in_komga"] and (not r["store_date"] or r["store_date"] <= today))
    upcoming = sum(1 for r in issues if not r["in_komga"] and r["store_date"] and r["store_date"] > today)
    soon = [r["store_date"] for r in issues
            if not r["in_komga"] and r["store_date"] and today < r["store_date"] <= cutoff]
    return {"owned": owned, "missing": missing, "upcoming": upcoming,
            "next_release": min(soon) if soon else None}


# --- connection tests ---

class TestKomgaRequest(BaseModel):
    url: str
    user: str
    password: str


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


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
            f"{client.base_url}/search/",
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
    cfg = db.get_config(DB_PATH)
    return {
        "komga_url":        cfg.get("komga_url", ""),
        "komga_user":       cfg.get("komga_user", ""),
        "komga_pass":       "",  # never expose password
        "komga_library_id": cfg.get("komga_library_id", ""),
        "metron_user":      cfg.get("metron_user", ""),
        "metron_pass":      "",  # never expose password
        "cv_api_key":       "",  # never expose key
        "cv_configured":    bool(cfg.get("cv_api_key", "")),
        "locg_user":        cfg.get("locg_user", ""),
        "locg_pass":        "",  # never expose password
        "locg_configured":  bool(cfg.get("locg_user", "") and cfg.get("locg_pass", "")),
        "sync_hours":       cfg.get("sync_hours", "5,12,17"),
    }


class ConfigRequest(BaseModel):
    komga_url:        str | None = None
    komga_user:       str | None = None
    komga_pass:       str | None = None
    komga_library_id: str | None = None
    metron_user:      str | None = None
    metron_pass:      str | None = None
    cv_api_key:       str | None = None
    locg_user:        str | None = None
    locg_pass:        str | None = None
    sync_hours:       str | None = None


@app.patch("/api/config")
def update_config(req: ConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None and v != ""}
    if "komga_url" in updates:
        updates["komga_url"] = _normalize_url(updates["komga_url"])
    db.set_config(updates, DB_PATH)
    return get_config()


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
        # LOCG-sourced: try to auto-link to Metron by title
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
    _sync_one(added)

    if req.on_pull_list:
        issues = db.get_issues_for_series(new_id, DB_PATH)
        today_str = str(date.today())
        for issue in issues:
            if not issue["in_komga"] and (not issue["store_date"] or issue["store_date"] <= today_str):
                db.queue_issue(new_id, issue["number"], DB_PATH)
        threading.Thread(target=_process_queue, daemon=True).start()

    return db.get_series_by_id(new_id, DB_PATH)


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

def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', s.lower())

@app.get("/api/search/metron")
def search_metron(q: str):
    metron = _metron()
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


@app.get("/api/search/locg")
def search_locg(q: str):
    locg_client = _locg()
    raw = locg_client.search_series(q) if locg_client else _locg_search_anon(q)
    return [{
        "id":         r["id"],
        "series":     r["title"],
        "publisher":  {"name": r["publisher"]} if r["publisher"] else None,
        "year_began": r["year"],
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
def pull_list(days: int = 90):
    return db.get_upcoming_issues(days, DB_PATH)


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
    except Exception:
        raise HTTPException(504)


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
    except Exception:
        raise HTTPException(404)


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
    except Exception:
        raise HTTPException(404)


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
    if result["promoted"] and _sync_all_job:
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
    groups = {"high": [], "medium": [], "low": [], "none": []}
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
            except Exception:
                pass

    threading.Thread(target=_bg_sync, daemon=True).start()
    return {"confirmed": confirmed, "errors": errors}


class RejectRequest(BaseModel):
    komga_series_id: str


@app.post("/api/match/reject")
def reject_match(req: RejectRequest):
    db.reject_candidate(req.komga_series_id, DB_PATH)
    return {"ok": True}


# --- filesystem browse ---

_COMICS_ROOT = os.path.realpath(os.environ.get("COMICS_ROOT", "/comics"))


@app.get("/api/fs/browse")
def browse_fs(path: str = ""):
    target = os.path.realpath(path or _COMICS_ROOT)
    if not target.startswith(_COMICS_ROOT):
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
        "parent": parent if parent.startswith(_COMICS_ROOT) and parent != target else None,
        "dirs": names,
    }


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
        if item["id"] in _dl_progress:
            item["progress"] = _dl_progress[item["id"]]
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
        if not issue["in_komga"] and (not issue["store_date"] or issue["store_date"] <= today):
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
    except Exception:
        raise HTTPException(404)


@app.get("/api/series/{series_id}/issues/{number}/queue-status")
def get_issue_queue_status(series_id: int, number: float):
    queue = db.get_queue(DB_PATH)
    q = next((q for q in queue if q["tracked_series_id"] == series_id and q["issue_number"] == number), None)
    if not q:
        return {"state": None}
    result = {"state": q["state"], "error": q.get("error")}
    if q["id"] in _dl_progress:
        result["progress"] = _dl_progress[q["id"]]
    return result


def _find_issue_file(folder_path: str, series_title: str, number: float) -> str | None:
    """Scan folder_path for a comic file matching issue number. Returns full path or None."""
    if not folder_path or not os.path.isdir(folder_path):
        return None
    for fname in os.listdir(folder_path):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}:
            continue
        parsed = _parse_issue_number(fname, series_title)
        if parsed is not None and parsed == number:
            return os.path.join(folder_path, fname)
    return None


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
        from kometa.locg_client import fetch_variants
        data = fetch_variants(locg_issue_id)
        return {"covers": data["covers"], "locg_issue_id": locg_issue_id}
    except Exception as e:
        raise HTTPException(502, detail=str(e))


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

    if issue.get("in_komga"):
        file_path = _find_issue_file(series.get("folder_path", ""), series["title"], number)
        if not file_path:
            raise HTTPException(404, detail="File not found on disk")
        try:
            from kometa.downloader import inject_covers
            added = inject_covers(file_path, req.selected, req.primary_id)
            return {"ok": True, "added": added}
        except Exception as e:
            raise HTTPException(500, detail=str(e))
    else:
        db.set_variant_prefs(series_id, number, req.selected, req.primary_id, DB_PATH)
        return {"ok": True, "queued": True}


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
