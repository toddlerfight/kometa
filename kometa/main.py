import os
from datetime import date
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kometa.komga_client import KomgaClient
from kometa.metron_client import MetronClient
from kometa.diff import compute_diff
from kometa.scheduler import start_scheduler
import kometa.db as db

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


def _sync_all_job():
    for s in db.get_all_series(DB_PATH):
        _sync_one(s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    start_scheduler(_sync_all_job)
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
            num in owned_set, book_map.get(num), path=DB_PATH,
        )
    db.mark_synced(series["id"], DB_PATH)
    return result


def _summary(series_id):
    rows = db.get_issues_for_series(series_id, DB_PATH)
    today = str(date.today())
    owned = sum(1 for r in rows if r["in_komga"])
    missing = sum(1 for r in rows if not r["in_komga"] and (not r["store_date"] or r["store_date"] <= today))
    upcoming = sum(1 for r in rows if not r["in_komga"] and r["store_date"] and r["store_date"] > today)
    return {"owned": owned, "missing": missing, "upcoming": upcoming}


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
        "sync_hours":       cfg.get("sync_hours", "5,12,17"),
    }


class ConfigRequest(BaseModel):
    komga_url:        str | None = None
    komga_user:       str | None = None
    komga_pass:       str | None = None
    komga_library_id: str | None = None
    metron_user:      str | None = None
    metron_pass:      str | None = None
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

    tracked_ids = {s["komga_series_id"] for s in db.get_all_series(DB_PATH)}

    items = [
        {
            "id": s["id"],
            "name": s["name"],
            "publisher": s.get("metadata", {}).get("publisher"),
            "year": s.get("metadata", {}).get("startYear"),
            "tracked": s["id"] in tracked_ids,
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


@app.get("/api/komga/series/{komga_series_id}/thumbnail")
def komga_series_thumbnail(komga_series_id: str):
    komga = _komga()
    r = komga.session.get(f"{komga.base_url}/api/v1/series/{komga_series_id}/thumbnail")
    r.raise_for_status()
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


app.mount("/", StaticFiles(directory="kometa/static", html=True), name="static")
