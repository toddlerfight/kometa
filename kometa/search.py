"""Universal search: one box, every kind of thing a person might be after.

GET /api/search?q=…&limit=8 answers with sections — series, people, reading
lists, and an issue jump ('batman 13' → that issue, openable straight in the
reader when it's on the shelf) — plus the single best hit across them all.

No network, ever: SQLite and the people index (kometa/people.py). The series
rows are cached for a few seconds — a palette asks on every keystroke.
"""
import logging
import re
import threading
import time

from fastapi import APIRouter

import kometa.db as db
from kometa import people
from kometa.naming import norm_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
CACHE_S = 10

_cache = {"at": 0.0, "path": None, "series": None}
_lock = threading.Lock()
_YEAR = re.compile(r"\s*\((?:19|20)\d{2}[^)]*\)\s*$")
_ISSUE_Q = re.compile(r"^(?P<series>.*?\D)\s*#?\s*(?P<num>\d+(?:\.\d+)?)\s*$")


def _key(title: str) -> str:
    """'The Amazing Spider-Man (2025)' → 'amazing spider man': what a person types."""
    return re.sub(r"^the ", "", norm_key(_YEAR.sub("", people.fold(title or ""))))


def _series(path) -> list[dict]:
    with _lock:
        if _cache["series"] is not None and _cache["path"] == path and time.time() - _cache["at"] < CACHE_S:
            return _cache["series"]
        summaries = db.get_all_series_summaries(path)
        rows = []
        for s in db.get_all_series(path):
            if s.get("kind") == "arc":
                continue
            c = summaries.get(s["id"], {})
            owned = c.get("owned") or 0
            rows.append({"id": s["id"], "title": s["title"], "publisher": s.get("publisher"),
                         "year_began": s.get("year_began"), "owned": owned,
                         "total": owned + (c.get("missing") or 0) + (c.get("upcoming") or 0),
                         "key": _key(s["title"])})
        _cache.update(at=time.time(), path=path, series=rows)
        return rows


def _card(s: dict, why=None) -> dict:
    return {"id": s["id"], "title": s["title"], "publisher": s["publisher"], "year_began": s["year_began"],
            "owned": s["owned"], "total": s["total"], "why": why, "cover": f"/api/series/{s['id']}/thumbnail"}


def _title_rank(key: str, q: str) -> int | None:
    """0 exact, 1 starts with, 2 a word starts with, 3 contains; None = no."""
    if not q:
        return None
    if key == q:
        return 0
    if key.startswith(q):
        return 1
    if f" {q}" in f" {key}":
        return 2
    if q in key:
        return 3
    return None


def _match_series(q: str, rows: list[dict]) -> list[tuple[int, dict]]:
    hits = [(r, s) for s in rows if (r := _title_rank(s["key"], q)) is not None]
    # owned runs first within a rank, then the shorter (closer) title
    hits.sort(key=lambda t: (t[0], -(t[1]["owned"] > 0), len(t[1]["key"]), t[1]["title"]))
    return hits


def issue_jump(q: str, path=None, rows: list[dict] | None = None) -> list[dict]:
    """'batman 13' / 'absolute batman #24' → that issue of the run it means: an
    exact title first, then starts-with, then contains; of the runs that fit,
    the one that HAS that number (owned first), else the newest."""
    path = path or DB_PATH
    m = _ISSUE_Q.match(q.strip())
    if not m:
        return []
    want = _key(m.group("series"))
    num = float(m.group("num"))
    if not want:
        return []
    rows = rows if rows is not None else _series(path)
    hits = _match_series(want, rows)
    if not hits:
        return []
    best_rank = hits[0][0]
    cands = [s for r, s in hits if r == best_rank] or [s for _, s in hits]
    out = []
    with db._connect(path) as conn:
        found = []
        for s in cands[:12]:
            i = conn.execute("SELECT owned, store_date FROM issue_status WHERE tracked_series_id = ? AND number = ?",
                             (s["id"], num)).fetchone()
            b = conn.execute("SELECT id, path FROM books WHERE tracked_series_id = ? AND number = ? ORDER BY "
                             "(path LIKE '%nnual%') , (path NOT LIKE '%.cbz') LIMIT 1", (s["id"], num)).fetchone()
            if i or b:
                found.append((s, bool(b) or bool(i and i["owned"]), b["id"] if b else None))
        if not found:
            return []
        found.sort(key=lambda t: (not t[1], -(t[0]["year_began"] or 0)))
        for s, owned, book_id in found[:3]:
            label = f"#{num:g}"
            out.append({"series_id": s["id"], "series": s["title"], "number": num, "label": label,
                        "book_id": book_id, "owned": bool(book_id) or owned,
                        "cover": f"/api/series/{s['id']}/issues/{num:g}/thumbnail"})
    return out


def _lists(q: str, path, limit: int) -> list[dict]:
    from kometa import readlists, related
    readlists.ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, name FROM reading_lists")]
    hits = []
    for r in rows:
        rank = _title_rank(norm_key(people.fold(r["name"])), q)
        if rank is not None:
            hits.append((rank, r))
    hits.sort(key=lambda t: (t[0], t[1]["name"]))
    warm = {}
    cached = related._lists_cache.get("value") if related._lists_cache.get("path") == path else None
    for res in cached or []:
        warm[res["id"]] = res
    out = []
    for _, r in hits[:limit]:
        res = warm.get(r["id"])
        if res is None:
            try:
                res = readlists.resolve(r["id"], path)
            except Exception:
                res = None
        first = next((b for e in (res or {}).get("entries", []) for b in e["books"]), None)
        cover = None
        if not first and res:
            item = next((e for e in res["entries"] if e.get("cover")), None)
            cover = item["cover"] if item else None
        out.append({"id": r["id"], "name": r["name"], "total": (res or {}).get("total", 0),
                    "owned": (res or {}).get("owned", 0), "cover_book_id": first["id"] if first else None,
                    "cover": cover})
    return out


def search(q: str, limit: int = 8, path=None) -> dict:
    path = path or DB_PATH
    q_raw = (q or "").strip()
    qk = _key(q_raw)
    out = {"q": q_raw, "top": None, "series": [], "people": [], "lists": [], "issues": []}
    if len(qk.replace(" ", "")) < 2:
        return out
    rows = _series(path)
    by_id = {s["id"]: s for s in rows}

    titled = _match_series(qk, rows)
    m = _ISSUE_Q.match(q_raw)
    if not titled and m:                      # 'batman 13': the runs the words name, under the issue jump
        titled = [(r + 1, s) for r, s in _match_series(_key(m.group("series")), rows)]
    seen = set()
    for _, s in titled:
        if len(out["series"]) >= limit:
            break
        out["series"].append(_card(s)); seen.add(s["id"])

    ppl = people.search(q_raw, path) if len(qk.replace(" ", "")) >= 3 else {"people": [], "series": {}}
    out["people"] = [{"id": p["id"], "name": p["name"], "roles": p["roles"], "count": p["count"]}
                     for p in ppl["people"][:limit]]
    for sid, why in sorted(ppl["series"].items(), key=lambda kv: by_id.get(kv[0], {}).get("title", "")):
        if len(out["series"]) >= limit:
            break
        if sid in seen or sid not in by_id:
            continue
        out["series"].append(_card(by_id[sid], why)); seen.add(sid)

    out["lists"] = _lists(qk, path, limit)
    out["issues"] = issue_jump(q_raw, path, rows)

    # the one best thing: exact title > an issue you can open > a prolific
    # person > the best series > a list
    exact = next((s for r, s in titled if r == 0), None)
    if exact:
        out["top"] = {"kind": "series", **_card(exact)}
    elif out["issues"] and out["issues"][0]["book_id"]:
        out["top"] = {"kind": "issue", **out["issues"][0]}
    elif out["people"] and out["people"][0]["count"] >= 3:
        out["top"] = {"kind": "person", **out["people"][0]}
    elif out["series"]:
        out["top"] = {"kind": "series", **out["series"][0]}
    elif out["issues"]:
        out["top"] = {"kind": "issue", **out["issues"][0]}
    elif out["lists"]:
        out["top"] = {"kind": "list", **out["lists"][0]}
    return out


@router.get("/api/search")
def api_search(q: str = "", limit: int = 8):
    return search(q, max(1, min(limit, 30)))
