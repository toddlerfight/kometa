"""The runs no catalogue pinned down, asked again (owner, 2026-10-18).

Comic Vine by name had 76 runs with no clean match and GCD 91 'none' or 'ambiguous' —
mostly because the run had no year (no on-sale dates on old books), or the name differed
by a space ('Gen 13'/'Gen13') or a binding word ('Dark Entries HC'). The matchers now take
the year from cover dates and look past spacing and binding (cvname.pick_volume,
harvest.gcd_match); this asks both again for those runs and PROPOSES — nothing is linked
until the owner has looked: a wrong match puts another book's credits on the shelf.

start() runs the asking on a thread (minutes: GCD is one request every 3 s); state() has
the proposals; apply(ids) links the approved ones the same way the ticks would.
"""
import logging
import re
import threading

from kometa import db
from kometa.db import DB_PATH

logger = logging.getLogger(__name__)

_state = {"state": "idle", "done": 0, "total": 0, "proposals": [], "error": None}
_lock = threading.Lock()


def _targets(path) -> list[dict]:
    from kometa import cvname
    cvname.ensure_tables(path)
    with db._connect(path) as conn:
        cv = {r[0] for r in conn.execute("SELECT tracked_series_id FROM cv_volume_match WHERE cv_volume_id IS NULL")}
        gcd = {r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE gcd_link IN ('none', 'ambiguous')")}
        hi = dict(conn.execute("SELECT tracked_series_id, MAX(number) FROM issue_status GROUP BY tracked_series_id").fetchall())
    out = []
    for sid in sorted(cv | gcd):
        s = db.get_series_by_id(sid, path)
        if s:
            out.append({"series": s, "cv": sid in cv, "gcd": sid in gcd, "hi": hi.get(sid)})
    return out


def _cv_proposal(cv, s: dict, path) -> dict | None:
    from kometa import cvname
    vid, why = cvname.find_volume(cv, s, path)
    return {"id": vid, "why": why} if vid else None


def _gcd_proposal(s: dict, hi, path) -> dict | None:
    from kometa import gcd_client, harvest
    results = []
    for q in harvest._gcd_queries(s["title"]):
        results = gcd_client.search_series(q)
        if results:
            break
    s = {**s, "_cover_year": harvest._cover_year(s["id"], path)}
    outcome, gid, cands = harvest.gcd_match(s, hi, results)
    if outcome == "ambiguous" and len(cands) > 1:
        gid = harvest.gcd_tiebreak(s, cands, path, gcd_client)
    if not gid:
        return None
    c = next((r for r in results if r.get("gcd_id") == gid), {})
    pub = gcd_client.publisher_name(c["publisher_id"]) if c.get("publisher_id") else None
    return {"id": gid, "why": f"{c.get('name')} ({c.get('year_began')}, {pub or '?'}, {c.get('issues')} issues)"}


def _run(path):
    from kometa.cvname import series_year
    from kometa.gcd_client import GCDUnavailable
    from kometa import sources
    try:
        targets = _targets(path)
        _state.update(total=len(targets))
        cv = sources.comicvine()
        gcd_ok = True
        for t in targets:
            s = t["series"]
            p = {"series_id": s["id"], "title": s["title"], "publisher": s.get("publisher"),
                 "year": series_year(s, path), "cv": None, "gcd": None}
            if t["cv"] and cv:
                try:
                    p["cv"] = _cv_proposal(cv, s, path)
                except Exception as e:
                    logger.info(f"Unpinned: Comic Vine for {s['title']!r}: {e}")
            if t["gcd"] and gcd_ok:
                try:
                    p["gcd"] = _gcd_proposal(s, t["hi"], path)
                except GCDUnavailable as e:
                    logger.info(f"Unpinned: GCD stopped: {e}")
                    gcd_ok = False
            if p["cv"] or p["gcd"]:
                _state["proposals"].append(p)
            _state["done"] += 1
        _state["state"] = "done"
    except Exception as e:
        logger.warning(f"Unpinned rematch failed: {e}")
        _state.update(state="failed", error=str(e))


def start(path=None) -> dict:
    """Ask again for every unpinned run, on a thread. → state()"""
    path = path or DB_PATH
    with _lock:
        if _state["state"] == "running":
            return state()
        _state.update(state="running", done=0, total=0, proposals=[], error=None)
    threading.Thread(target=_run, args=(path,), daemon=True, name="unpinned").start()
    return state()


def state() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _state.items()}


def apply(series_ids: list[int], path=None) -> dict:
    """Link the approved proposals: Comic Vine's volume and/or GCD's run, as the ticks would.
    → {'cv': n, 'gcd': n}"""
    path = path or DB_PATH
    from kometa.cvname import _now
    want = {int(i) for i in series_ids}
    got = {"cv": 0, "gcd": 0}
    for p in _state["proposals"]:
        if p["series_id"] not in want:
            continue
        if p.get("cv"):
            with db._connect(path) as conn:
                conn.execute("UPDATE cv_volume_match SET cv_volume_id = ?, why = ?, at = ?, issues_at = NULL "
                             "WHERE tracked_series_id = ?", (p["cv"]["id"], "asked again: " + p["cv"]["why"], _now(),
                                                             p["series_id"]))
            got["cv"] += 1
        if p.get("gcd"):
            db.set_gcd_link(p["series_id"], "linked", p["gcd"]["id"], [], path)
            got["gcd"] += 1
    logger.info(f"Unpinned runs linked: {got} for {sorted(want)}")
    return got
