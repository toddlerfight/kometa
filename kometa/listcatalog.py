"""The community's reading lists: DieselTech's CBL repo on GitHub, ~1,700 .cbl
orders by publisher and character, searchable from the apps so a reader adds a
list by name instead of hunting down a file (the owner, 2026-10-26: lists are
per user, and testers need a way to make their own).

The repo's whole tree is ONE GitHub call (git/trees?recursive=1, untruncated at
this size), kept for a day. Adding a list fetches its raw file and imports it
as the asking reader's own — the same path as a pasted link.
"""
import logging
import re
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kometa import readlists

logger = logging.getLogger(__name__)
router = APIRouter()

REPO = "DieselTech/CBL-ReadingLists"
BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
BLOB = f"https://github.com/{REPO}/blob/{BRANCH}/"
TTL_S = 24 * 3600
MAX_RESULTS = 40

_index = {"at": 0.0, "paths": []}
_lock = threading.Lock()


def _fetch_tree(session=None) -> list[str]:
    import requests
    r = (session or requests).get(TREE_URL, timeout=20, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return sorted(t["path"] for t in r.json().get("tree", []) if t.get("type") == "blob" and t["path"].endswith(".cbl"))


def index(session=None) -> list[str]:
    """Every .cbl path in the repo. A day old at most; when GitHub won't answer,
    the last good copy (or nothing, the first time)."""
    with _lock:
        if _index["paths"] and time.time() - _index["at"] < TTL_S:
            return _index["paths"]
        try:
            _index.update(at=time.time(), paths=_fetch_tree(session))
            logger.info(f"List catalogue: {len(_index['paths'])} lists in {REPO}")
        except Exception as e:
            logger.warning(f"List catalogue: {REPO} didn't answer: {e}")
            if not _index["paths"]:
                raise
        return _index["paths"]


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _entry(path: str) -> dict:
    parts = path[:-4].split("/")
    return {"path": path, "name": parts[-1], "folder": " › ".join(parts[:-1])}


def search(q: str, paths: list[str]) -> list[dict]:
    """Every word of the query somewhere in the list's path (publisher, character,
    title). A hit in the list's own name ranks above one only in its folders,
    then the shorter name — 'Hellboy' finds the Hellboy orders before every list
    filed under Dark Horse."""
    want = _words(q)
    if not want:
        return []
    hits = []
    for p in paths:
        e = _entry(p)
        name, everything = " ".join(_words(e["name"])), " ".join(_words(p))
        if all(w in everything for w in want):
            in_name = sum(w in name for w in want)
            hits.append((-in_name, len(e["name"]), e["name"].lower(), e))
    hits.sort(key=lambda h: h[:3])
    return [h[3] for h in hits[:MAX_RESULTS]]


def add(path: str, fetch=None) -> int:
    """Import one of the repo's lists as the asking reader's own. Only paths the
    index holds: the server never fetches a path a client made up."""
    if path not in set(index()):
        raise KeyError(path)
    body = (fetch or readlists.fetch_public)(RAW + quote(path), readlists.MAX_CBL_BYTES)
    return readlists.import_cbl(body, source_ref=BLOB + quote(path))


# --- API --------------------------------------------------------------------------
@router.get("/api/listcatalog")
def api_search(q: str = ""):
    try:
        paths = index()
    except Exception:
        raise HTTPException(503, "Couldn't reach the community lists on GitHub")
    return {"results": search(q, paths), "total": len(paths)}


class AddRequest(BaseModel):
    path: str


@router.post("/api/listcatalog/import")
def api_add(req: AddRequest):
    try:
        return {"id": add(req.path)}
    except KeyError:
        raise HTTPException(404, "That list isn't in the community lists")
    except readlists.CblError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch that list: {e}")
