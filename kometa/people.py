"""People search: find series by who made them.

One index of every credited person on the shelf, built from the two places
credits live: series_signals (Metron's creators per run, with ids) and
issue_record (per-issue credits from Metron, LOCG, ComicInfo and Komga — the
last two have names only). A person is keyed by their normalised name, so
'José Villarrubia' from Metron and 'Jose Villarrubia' from a ComicInfo file
are one person, and the Metron id rides along when any source had it.

Editors are left out: a search for a name should find what they wrote or
drew, not every book their office signed off.

The index is a few thousand rows built from SQLite only — no disk, no network.
Kept warm for five minutes and rebuilt in the background when stale.
"""
import json
import logging
import re
import threading
import time
import unicodedata

from fastapi import APIRouter

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
TTL_S = 300
MAX_PEOPLE = 12
# how roles read, and in which order a person's roles are listed
ROLE_ORDER = ["writer", "story", "plot", "script", "artist", "penciller", "pencils", "inker", "inks",
              "colorist", "colors", "letterer", "cover", "translator"]
SKIP_ROLE = re.compile(r"editor|production|design|logo|consult", re.I)

_cache = {"at": 0.0, "path": None, "value": None}
_lock = threading.Lock()


def fold(s: str) -> str:
    """Accent-, case- and punctuation-free: 'José' = 'Jose', 'J.H. Williams III' = 'j h williams iii'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _role(r: str) -> str:
    r = (r or "").strip().lower()
    return {"penciler": "penciller", "pencils": "penciller", "inks": "inker", "colors": "colorist",
            "cover artist": "cover", "covers": "cover", "story": "writer", "script": "writer", "plot": "writer"}.get(r, r)


def _add(idx: dict, name: str, role: str, sid: int, pid):
    if not name or not sid or SKIP_ROLE.search(role or ""):
        return
    key = fold(name)
    if not key:
        return
    p = idx.setdefault(key, {"name": name, "id": None, "series": {}, "names": {}})
    p["names"][name] = p["names"].get(name, 0) + 1
    if pid and not p["id"]:
        p["id"] = int(pid)
    roles = {_role(r) for r in re.split(r"\s*[,/&]\s*", role or "") if r.strip()} or {""}   # LOCG: 'Story, Writer'
    p["series"].setdefault(sid, set()).update(r for r in roles if r)


def build(path=None) -> dict:
    path = path or DB_PATH
    idx: dict = {}
    from kometa import record
    record.ensure_tables(path)                # a fresh DB has no issue_record yet
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS series_signals (tracked_series_id INTEGER PRIMARY KEY,
            creators_json TEXT, arcs_json TEXT, fetched_at TEXT DEFAULT (datetime('now')))""")
        live = {r["id"] for r in conn.execute("SELECT id FROM tracked_series WHERE COALESCE(kind, 'series') != 'arc'")}
        for r in conn.execute("SELECT tracked_series_id, creators_json FROM series_signals WHERE creators_json NOT IN ('[]', '')"):
            if r["tracked_series_id"] not in live:
                continue
            for c in json.loads(r["creators_json"] or "[]"):
                _add(idx, c.get("name"), c.get("role"), r["tracked_series_id"], c.get("id"))
        for r in conn.execute("SELECT tracked_series_id, credits_json FROM issue_record WHERE credits_json NOT IN ('[]', '')"):
            if r["tracked_series_id"] not in live:
                continue
            try:
                credits = json.loads(r["credits_json"] or "[]")
            except ValueError:
                continue
            for c in credits:
                _add(idx, c.get("name"), c.get("role"), r["tracked_series_id"], c.get("metron_creator_id") or c.get("id"))
    for p in idx.values():
        p["name"] = max(p["names"].items(), key=lambda kv: (kv[1], any(ord(ch) > 127 for ch in kv[0])))[0]   # the usual spelling, accents kept
        del p["names"]
    return idx


def _refresh(path) -> dict:
    with _lock:
        fresh = build(path)
        _cache.update(at=time.time(), path=path, value=fresh)
        return fresh


def index(path=None) -> dict:
    """Serve-stale: an expired index is returned while one thread rebuilds it."""
    path = path or DB_PATH
    if _cache["value"] is not None and _cache["path"] == path:
        if time.time() - _cache["at"] >= TTL_S and not _lock.locked():
            threading.Thread(target=_refresh, args=(path,), daemon=True).start()
        return _cache["value"]
    return _refresh(path)


def _roles_label(roles: set) -> list[str]:
    rank = {r: i for i, r in enumerate(ROLE_ORDER)}
    return [r.capitalize() for r in sorted(roles, key=lambda r: (rank.get(r, 99), r))]


def search(q: str, path=None) -> dict:
    """People whose name holds every word of q as a word prefix ('pope', 'paul p',
    'villar'), most prolific on the shelf first; and every series they're on,
    with the reason a card can show: 'Artist: Paul Pope'."""
    words = fold(q).split()
    if not words or len("".join(words)) < 3:
        return {"people": [], "series": {}}
    def matches(parts: list[str]) -> bool:
        free = list(parts)                  # each typed word claims its own name part: 'paul p' ≠ Paul Mounts
        for w in sorted(words, key=len, reverse=True):
            hit = next((x for x in free if x.startswith(w)), None)
            if hit is None:
                return False
            free.remove(hit)
        return True
    hits = [p for key, p in index(path).items() if matches(key.split())]
    hits.sort(key=lambda p: (-len(p["series"]), p["name"]))
    people, why = [], {}
    for p in hits:
        roles = set().union(*p["series"].values()) if p["series"] else set()
        if len(people) < MAX_PEOPLE:
            people.append({"id": p["id"], "name": p["name"], "roles": _roles_label(roles),
                           "series_ids": sorted(p["series"]), "count": len(p["series"])})
        for sid, rs in p["series"].items():
            why.setdefault(sid, f"{_roles_label(rs)[0]}: {p['name']}" if rs else p["name"])
    return {"people": people, "series": why}


@router.get("/api/search/people")
def api_search_people(q: str = ""):
    out = search(q)
    return {"people": out["people"], "series": {str(k): v for k, v in out["series"].items()}}
