import os
import json
import threading
from datetime import date
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.komga_client import KomgaClient
from kometa.metron_client import MetronClient
from kometa.comicvine_client import ComicVineClient
from kometa.getcomics_client import GetComicsClient
from kometa.diff import compute_diff
from kometa.scheduler import start_scheduler
from kometa.apns import send_push
import kometa.db as db
import kometa.downloader as downloader
import kometa.matcher as matcher

DB_PATH = os.environ.get("KOMETA_DB", "/data/kometa.db")


def _komga() -> KomgaClient:
    cfg = db.get_config(DB_PATH)
    return KomgaClient(
        base_url=cfg.get("komga_url", ""),
        auth=(cfg.get("komga_user", ""), cfg.get("komga_pass", "")),
        library_id=cfg.get("komga_library_id", ""),
    )


def _metron() -> MetronClient:
    cfg = db.get_config(DB_PATH)
    return MetronClient(auth=(cfg.get("metron_user", ""), cfg.get("metron_pass", "")))


def _comicvine() -> ComicVineClient | None:
    key = db.get_config(DB_PATH).get("cv_api_key", "")
    return ComicVineClient(key) if key else None


def _sync_all_job():
    for s in db.get_all_series(DB_PATH):
        _sync_one(s)


def _komga_scan():
    _komga().scan_library()


def _notify_acquired(title: str, issue_number: float):
    cfg = db.get_config(DB_PATH)
    key_pem  = cfg.get("apns_key_pem", "")
    key_id   = cfg.get("apns_key_id", "")
    team_id  = cfg.get("apns_team_id", "")
    bundle_id = cfg.get("apns_bundle_id", "")
    if not all([key_pem, key_id, team_id, bundle_id]):
        return
    tokens = db.get_push_tokens(DB_PATH)
    if not tokens:
        return
    num = int(issue_number) if issue_number == int(issue_number) else issue_number
    send_push(
        tokens=tokens,
        title="Acquired",
        body=f"{title} #{num}",
        data={"type": "acquired"},
        key_pem=key_pem,
        key_id=key_id,
        team_id=team_id,
        bundle_id=bundle_id,
        sandbox=cfg.get("apns_sandbox", "0") == "1",
    )


def _process_queue():
    items = db.get_queued_items(DB_PATH)
    if not items:
        return
    gc = GetComicsClient()
    for item in items:
        qid = item["id"]
        db.update_queue_state(qid, "searching", path=DB_PATH)
        try:
            # get store_date from issue_status for year in filename
            issues = db.get_issues_for_series(item["tracked_series_id"], DB_PATH)
            issue_row = next((i for i in issues if i["number"] == item["issue_number"]), None)
            store_date = issue_row["store_date"] if issue_row else None

            dl_url, hint_filename = gc.search(item["title"], item["issue_number"])
            if not dl_url:
                db.update_queue_state(qid, "not_found", error="No result on GetComics", path=DB_PATH)
                continue

            db.update_queue_state(qid, "downloading", source_url=dl_url, path=DB_PATH)
            dest = downloader.download_issue(
                url=dl_url,
                title=item["title"],
                publisher=item["publisher"],
                issue_number=item["issue_number"],
                store_date=store_date,
                hint_filename=hint_filename,
                komga_scan_fn=_komga_scan,
            )
            db.update_queue_state(qid, "done", filename=dest, path=DB_PATH)
            threading.Thread(
                target=_notify_acquired,
                args=(item["title"], item["issue_number"]),
                daemon=True,
            ).start()
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
    start_scheduler(_sync_all_job, _process_queue, _sweep_missing)
    yield


app = FastAPI(lifespan=lifespan)


# --- sync logic ---

def _sync_one(series: dict):
    komga = _komga()
    metron = _metron()
    books = komga.get_books(series["komga_series_id"])
    issues = metron.get_issues(series["metron_series_id"])
    result = compute_diff(books, issues, date.today())

    owned_set = set(result["owned"])
    book_map = {}
    for b in books:
        n = b["metadata"]["numberSort"]
        if n is not None:
            book_map.setdefault(float(n), b["id"])

    for issue in issues:
        num = float(issue["number"])
        db.upsert_issue_status(
            series["id"], num, issue["store_date"],
            num in owned_set, book_map.get(num),
            metron_image=issue.get("image"),
            path=DB_PATH,
        )
    db.mark_synced(series["id"], DB_PATH)
    return result


def _summary(series_id):
    from datetime import timedelta
    rows = db.get_issues_for_series(series_id, DB_PATH)
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    owned = sum(1 for r in rows if r["in_komga"])
    missing = sum(1 for r in rows if not r["in_komga"] and (not r["store_date"] or r["store_date"] <= today))
    upcoming = sum(1 for r in rows if not r["in_komga"] and r["store_date"] and r["store_date"] > today)
    soon = [r["store_date"] for r in rows
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
    return [dict(s, **_summary(s["id"])) for s in series]


@app.get("/api/series/{series_id}")
def get_series(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    issues = db.get_issues_for_series(series_id, DB_PATH)
    return dict(s, issues=issues, **_summary(series_id))


class AddSeriesRequest(BaseModel):
    komga_id: str
    metron_id: int


@app.post("/api/series", status_code=201)
def add_series(req: AddSeriesRequest):
    komga = _komga()
    metron = _metron()
    komga_series = komga.get_series(req.komga_id)
    metron_series = metron.get_series(req.metron_id)
    db.add_series(
        req.komga_id, req.metron_id,
        title=komga_series["name"],
        publisher=komga_series.get("metadata", {}).get("publisher"),
        year_began=metron_series.get("year_began"),
        path=DB_PATH,
    )
    series = db.get_all_series(DB_PATH)
    added = next(s for s in series if s["komga_series_id"] == req.komga_id)
    _sync_one(added)
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
    return _komga().search_series(q)


@app.get("/api/search/metron")
def search_metron(q: str):
    return _metron().search_series(q)


# --- sync ---

@app.post("/api/sync")
def sync_all():
    results = {}
    for s in db.get_all_series(DB_PATH):
        results[s["id"]] = _sync_one(s)
    return results


@app.post("/api/sync/{series_id}")
def sync_one(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    return _sync_one(s)


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
    r = komga.session.get(f"{komga.base_url}/api/v1/series/{s['komga_series_id']}/thumbnail")
    r.raise_for_status()
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


@app.get("/api/book/{book_id}/thumbnail")
def book_thumbnail(book_id: str):
    komga = _komga()
    r = komga.session.get(f"{komga.base_url}/api/v1/books/{book_id}/thumbnail")
    r.raise_for_status()
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


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
    r = komga.session.get(f"{komga.base_url}/api/v1/series/{komga_series_id}/thumbnail")
    r.raise_for_status()
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

        r = metron.session.get(img_url, timeout=10)
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
    if matcher.get_state()["running"]:
        return {"ok": False, "message": "Scan already running"}
    started = matcher.start(_komga, _metron, DB_PATH, sync_callback=_sync_all_job)
    return {"ok": started, "state": matcher.get_state()}


@app.post("/api/match/retry-empty")
def retry_empty_scan():
    """Re-scan the none-confidence candidates that got no API results (rate-limit victims)."""
    if matcher.get_state()["running"]:
        return {"ok": False, "message": "Scan already running"}
    started = matcher.start(_komga, _metron, DB_PATH, sync_callback=_sync_all_job, retry_empty=True)
    return {"ok": started, "state": matcher.get_state()}


@app.get("/api/match/status")
def scan_status():
    state = matcher.get_state()
    summary = db.get_candidates_summary(DB_PATH)
    counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for row in summary:
        if row["status"] == "pending":
            counts[row["confidence"]] = row["cnt"]
    return {**state, "counts": counts}


@app.get("/api/match/candidates")
def get_candidates():
    rows = db.get_pending_candidates(DB_PATH)
    groups = {"high": [], "medium": [], "low": [], "none": []}
    for r in rows:
        c = dict(r)
        c["candidates"] = json.loads(c["candidates_json"]) if c["candidates_json"] else []
        del c["candidates_json"]
        groups[c["confidence"]].append(c)
    return groups


class ConfirmRequest(BaseModel):
    komga_series_id: str
    metron_id: int


@app.post("/api/match/confirm")
def confirm_match(req: ConfirmRequest):
    existing = {s["komga_series_id"] for s in db.get_all_series(DB_PATH)}
    if req.komga_series_id not in existing:
        komga  = _komga()
        metron = _metron()
        ks = komga.get_series(req.komga_series_id)
        ms = metron.get_series(req.metron_id)
        db.add_series(
            req.komga_series_id, req.metron_id,
            title      = ks["name"],
            publisher  = ks.get("metadata", {}).get("publisher"),
            year_began = ms.get("year_began"),
            path       = DB_PATH,
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
    komga  = _komga()
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
                title      = ks["name"],
                publisher  = ks.get("metadata", {}).get("publisher"),
                year_began = ms.get("year_began"),
                path       = DB_PATH,
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


# --- monitor status ---

class MonitorRequest(BaseModel):
    status: str  # monitored | collected | ignored


@app.patch("/api/series/{series_id}/monitor")
def set_monitor(series_id: int, req: MonitorRequest):
    if req.status not in ("monitored", "collected", "ignored"):
        raise HTTPException(400, "status must be monitored, collected, or ignored")
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    db.set_monitor_status(series_id, req.status, DB_PATH)
    return db.get_series_by_id(series_id, DB_PATH)


# --- download queue ---

@app.get("/api/queue")
def get_queue():
    return db.get_queue(DB_PATH)


@app.delete("/api/queue/{queue_id}", status_code=204)
def delete_queue_item(queue_id: int):
    db.remove_queue_item(queue_id, DB_PATH)


@app.post("/api/queue/{queue_id}/retry", status_code=200)
def retry_queue_item(queue_id: int):
    db.update_queue_state(queue_id, "queued", error=None, path=DB_PATH)
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
    """Manually trigger the missing-issue sweep for monitored series."""
    threading.Thread(target=_sweep_missing, daemon=True).start()
    return {"ok": True}


# --- push / widget ---

class PushRegisterRequest(BaseModel):
    token: str


@app.post("/api/push/register", status_code=204)
def register_push(req: PushRegisterRequest):
    db.register_push_token(req.token, DB_PATH)


class ApnsConfigRequest(BaseModel):
    apns_key_pem:  str | None = None
    apns_key_id:   str | None = None
    apns_team_id:  str | None = None
    apns_bundle_id: str | None = None
    apns_sandbox:  str | None = None  # "0" or "1"


@app.patch("/api/config/apns", status_code=204)
def update_apns_config(req: ApnsConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    db.set_config(updates, DB_PATH)


@app.get("/api/widget/upcoming")
def widget_upcoming():
    """Pull-list issues releasing this week, with thumbnail URLs."""
    cfg = db.get_config(DB_PATH)
    komga_url = cfg.get("komga_url", "").rstrip("/")
    rows = db.get_pull_list_this_week(DB_PATH)
    result = []
    for r in rows:
        thumb = None
        if r["komga_book_id"]:
            thumb = f"{komga_url}/api/v1/books/{r['komga_book_id']}/thumbnail"
        elif r["komga_series_id"]:
            thumb = f"{komga_url}/api/v1/series/{r['komga_series_id']}/thumbnail"
        elif r["metron_image"]:
            thumb = r["metron_image"]
        result.append({
            "title":      r["title"],
            "number":     r["number"],
            "store_date": r["store_date"],
            "in_komga":   bool(r["in_komga"]),
            "thumbnail":  thumb,
        })
    return result


@app.get("/api/widget/recent")
def widget_recent(limit: int = 10):
    """Recently acquired issues, with thumbnail URLs."""
    cfg = db.get_config(DB_PATH)
    komga_url = cfg.get("komga_url", "").rstrip("/")
    rows = db.get_recent_acquisitions(limit, DB_PATH)
    result = []
    for r in rows:
        thumb = None
        if r["komga_book_id"]:
            thumb = f"{komga_url}/api/v1/books/{r['komga_book_id']}/thumbnail"
        elif r["komga_series_id"]:
            thumb = f"{komga_url}/api/v1/series/{r['komga_series_id']}/thumbnail"
        elif r["metron_image"]:
            thumb = r["metron_image"]
        result.append({
            "title":      r["title"],
            "number":     r["issue_number"],
            "acquired_at": r["updated_at"],
            "thumbnail":  thumb,
        })
    return result


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
