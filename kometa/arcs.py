"""Story-arc routes and machinery — the LENS model's API surface.

Extracted from main.py. An arc owns nothing: it's a cross-title reading-order
overlay whose issues live in their real series' folders (see kometa/arc.py for
the matching logic). This module holds the bricks: ownership resolution against
disk/Komga (brick A), auto-tracking of participating series (brick B, publisher-
gated), stamping arc issues into their runs (brick C), plus discovery, populate,
open-issue, fulfill, and the readlist builder.

Imports are strictly one-way (db / sources / sync / acquisition / naming /
arc / locg_client). main.py imports back only the router plus the three
functions its own routes need (_owned_collection, _series_cv_volume, _add_arc)
— arcs never imports main.
"""
import os
import re
import logging
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db
from kometa.sources import (
    komga as _komga, comics_root as _comics_root,
    comicvine as _comicvine, wikipedia as _wikipedia,
)
from kometa.sync import (
    sync_one as _sync_one, sync_one_guarded,
    _best_komga_match, _komga_all_series,
)
from kometa.acquisition import _process_queue
from kometa.naming import _resolve_dir, norm_key as _norm, parse_issue_number as _parse_issue_number
from kometa.models import AddSeriesRequest

logger = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = db.DB_PATH

_COMIC_EXTS = (".cbz", ".cbr", ".cb7", ".cbt", ".pdf")


def _arc_collection(arc_title: str, komga, all_series=None):
    """The Komga series that collects an arc (its trade edition), matched by title —
    owned-as-a-collection regardless of whether that collection is separately tracked.
    Returns the Komga series dict or None."""
    if not komga:
        return None
    from kometa.arc import titles_match
    try:
        series = all_series if all_series is not None else komga.get_all_series()
    except Exception:
        return None
    return next((x for x in series if titles_match(x.get("name", ""), arc_title)), None)


def _collection_info(coll, komga) -> dict | None:
    """Shape a Komga collection series for the API: name, book count, and the tracked
    series id (if any) so the arc page can link to it."""
    if not coll:
        return None
    try:
        books = len(komga.get_books(coll["id"]))
    except Exception:
        books = None
    tracked = db.find_series_by_title(coll.get("name", ""), DB_PATH)
    return {"name": coll["name"], "komga_series_id": coll["id"], "books": books,
            "series_id": tracked["id"] if tracked else None}


def _folder_has_comics(folder: str | None) -> bool:
    """True if the folder exists on disk and holds at least one comic file. The disk
    is the sole ownership authority (spec) — this is what 'we own it' actually means."""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        for root, _dirs, files in os.walk(folder):
            if any(f.lower().endswith(_COMIC_EXTS) for f in files):
                return True
    except OSError:
        pass
    return False


def _owned_collection(arc_title: str):
    """A trade/series we ACTUALLY own on disk that collects this arc — title-matched,
    folder must exist with comic files. Replaces the old Komga-title match, which voted
    ownership off metadata alone (the 'collected' lie). Returns the tracked series dict
    or None."""
    from kometa.arc import titles_match
    for s in db.get_all_series(DB_PATH):
        if s.get("kind") == "arc":
            continue
        if titles_match(s.get("title", ""), arc_title) and _folder_has_comics(s.get("folder_path")):
            return s
    return None


def _resolve_arc_ownership(arc_series_id: int) -> dict:
    """Cross-title ownership for an arc (the lens model): match each reading-order
    issue (source_title + number) against ITS OWN title's Komga series, stamping
    komga_book_id + owned. An arc spans titles, so this walks several Komga series,
    not one — that's why a single 'the arc's Komga series' lookup was wrong."""
    from kometa.arc import titles_match
    komga = _komga()
    s = db.get_series_by_id(arc_series_id, DB_PATH)
    rows = db.get_arc_reading_order(arc_series_id, DB_PATH)
    if not komga or not rows:
        return {"owned": 0, "total": len(rows), "collection": None}
    all_series = komga.get_all_series()

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return _parse_issue_number(str(v or ""))

    # (A) Singles: match each reading-order issue to a book in ITS title's Komga
    # series. One book_map (issue number -> book id) per source title, fetched once.
    book_maps: dict[str, dict] = {}
    resolved = []
    for r in rows:
        st = r.get("source_title") or ""
        if st not in book_maps:
            ks = next((x for x in all_series if titles_match(x.get("name", ""), st)), None)
            bm = {}
            if ks:
                for b in komga.get_books(ks["id"]):
                    if b.get("media", {}).get("status") == "ERROR":
                        continue
                    n = _parse_issue_number(b.get("name", ""), st)
                    if n is not None and n not in bm:
                        bm[n] = b["id"]
            book_maps[st] = bm
        n = _num(r.get("number"))
        book_id = book_maps[st].get(n) if n is not None else None
        resolved.append((r["reading_order"], book_id, 1 if book_id else 0))
    db.set_arc_ownership(arc_series_id, resolved, DB_PATH)

    # (B) Collected edition: a KOMGA series named after the arc (its trade edition),
    # whether or not that collection is separately tracked. Vintage arcs are usually
    # owned this way, not as singles; the readlist falls back to its volumes.
    coll = _arc_collection(s["title"], komga, all_series)
    return {"owned": sum(1 for _, b, o in resolved if o), "total": len(rows),
            "collection": _collection_info(coll, komga)}


@router.post("/api/series/{series_id}/resolve-arc")
def resolve_arc(series_id: int):
    """Re-match an arc's reading order against Komga (after grabbing issues/trades),
    refreshing the owned counts shown on the arc page."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s or s.get("kind") != "arc":
        raise HTTPException(404, "Not a story arc")
    return _resolve_arc_ownership(series_id)


@router.post("/api/series/{series_id}/readlist")
def build_arc_readlist(series_id: int):
    """Build (or rebuild) a Komga readlist from a story arc's reading order. The arc
    spans titles, so it gathers the matched book across EVERY participating Komga
    series, in reading order — re-resolving ownership first so it's fresh."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s or s.get("kind") != "arc":
        raise HTTPException(404, "Not a story arc")
    komga = _komga()
    if not komga:
        raise HTTPException(400, "Komga not configured")
    _resolve_arc_ownership(series_id)
    rows = db.get_arc_reading_order(series_id, DB_PATH)
    coll = _arc_collection(s["title"], komga)
    # Prefer issue-exact order when EVERY issue resolved to a single; otherwise fall
    # back to the collected edition's volumes (the common vintage case); else whatever
    # singles we did match.
    if rows and all(r.get("komga_book_id") for r in rows):
        book_ids, mode = [r["komga_book_id"] for r in rows], "singles"
    elif coll:
        book_ids, mode = [b["id"] for b in komga.get_books(coll["id"])], "collection"
    else:
        book_ids, mode = [r["komga_book_id"] for r in rows if r.get("komga_book_id")], "partial"
    if not book_ids:
        raise HTTPException(404, "None of this arc's issues are in Komga yet — grab them first")
    result = komga.create_or_update_readlist(
        s["title"], book_ids, summary=f"Reading order for {s['title']} — built by Kometa.")
    return {"name": s["title"], "books": len(book_ids), "total": len(rows),
            "mode": mode, "updated": result.get("updated", False)}


def _series_cv_volume(series: dict):
    """The series' CV volume id (cached on the row), resolved via volume search by
    base title + year — so arc discovery scopes to the right RUN (Batman 1940 vs
    2025), not every same-named volume."""
    if series.get("cv_volume_id"):
        return series["cv_volume_id"]
    cv = _comicvine()
    if not cv:
        return None
    from kometa.arc import base_series_title, titles_match
    base = base_series_title(series["title"])
    yr = series.get("year_began")
    try:
        vols = cv.search_volumes(base, limit=15)
    except Exception:
        return None
    match = (next((v for v in vols if titles_match(v.get("name", ""), base) and v.get("year") == yr), None)
             if yr else None) \
        or next((v for v in vols if titles_match(v.get("name", ""), base)), None)
    if match and match.get("cv_volume_id"):
        db.set_series_cv_volume(series["id"], str(match["cv_volume_id"]), DB_PATH)
        return str(match["cv_volume_id"])
    return None


def _discover_arcs(series: dict) -> list[dict]:
    """Discovered arcs for a series, cached ~7 days. PRIMARY = ComicVine, scoped to
    the series' RUN (so 'Batman 2025' doesn't inherit Batman 1940's canon). When CV
    knows the run, its scoped result is authoritative (even if empty — a new book has
    no arcs). Only when CV doesn't recognize the run at all do we fall back to the
    messy-but-broad Wikipedia."""
    cached = db.get_arc_discovery(series["id"], DB_PATH)
    if cached and cached["age"] < 7 * 86400:
        return cached["arcs"]
    cv = _comicvine()
    if cv:
        vid = _series_cv_volume(series)
        if vid:
            try:
                arcs = [{"name": a["name"], "cv_arc_id": a["cv_arc_id"],
                         "image": a.get("image"), "source": "comicvine"}
                        for a in cv.discover_arcs(series["title"], vid)]
            except Exception as e:
                logger.warning(f"CV arc discovery failed for {series['title']!r}: {e}")
                arcs = []
            db.set_arc_discovery(series["id"], arcs, DB_PATH)
            return arcs
    try:  # CV doesn't recognize this run (new/indie) -> Wikipedia
        arcs = [{"name": a["name"], "first_issue": a["first_issue"],
                 "last_issue": a["last_issue"], "source": "wikipedia"}
                for a in _wikipedia().discover_arcs(series["title"], series.get("year_began"))]
    except Exception as e:
        logger.warning(f"Wikipedia arc discovery failed for {series['title']!r}: {e}")
        return cached["arcs"] if cached else []
    db.set_arc_discovery(series["id"], arcs, DB_PATH)
    return arcs


@router.get("/api/series/{series_id}/arcs")
def get_series_arcs(series_id: int):
    """The series' Arcs tab: every story arc it participates in — discovered from
    Wikipedia (the arcs come with the series, like its issues + trades) and merged
    with any already-tracked arcs (which carry live owned counts)."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    # Arcs are a ComicVine feature — toggle off means no arcs, period. Return empty
    # instead of leaking the Wikipedia discovery fallback out the API. The UI already
    # hides the tab; this shuts the direct-call door so "off" actually means off.
    if db.get_config(DB_PATH).get("comicvine_enabled", "1") == "0":
        return {"arcs": []}
    from kometa.arc import arc_includes_series, titles_match
    svid = _series_cv_volume(s)        # the series' run, resolved + cached
    if svid:
        s["cv_volume_id"] = svid       # so _discover_arcs doesn't re-resolve

    def _arc_matches(a):
        # Volume-aware when both sides carry it (Knightfall's vol 796 won't match the
        # 2025 book); fall back to title match for legacy arcs / unresolved volumes.
        if svid and a.get("cv_volume_ids"):
            return svid in a["cv_volume_ids"]
        return arc_includes_series(a["source_titles"], s["title"])

    tracked = [a for a in db.get_all_arcs(DB_PATH) if _arc_matches(a)]

    def _match_tracked(name):
        return next((a for a in tracked
                     if titles_match(a["title"], name) or _norm(name) in _norm(a["title"])), None)

    out, used = [], set()
    for d in _discover_arcs(s):
        t = _match_tracked(d["name"])
        if t:
            used.add(t["id"])
            out.append({"name": t["title"], "id": t["id"], "tracked": True,
                        "issue_count": t["issue_count"], "owned_count": t["owned_count"],
                        "source_titles": t["source_titles"], "image": d.get("image")})
        else:
            out.append({**d, "tracked": False})
    # tracked arcs Wikipedia didn't surface (e.g. CV-added, or a coverage gap)
    for a in tracked:
        if a["id"] not in used:
            out.append({"name": a["title"], "id": a["id"], "tracked": True,
                        "issue_count": a["issue_count"], "owned_count": a["owned_count"],
                        "source_titles": a["source_titles"]})
    return {"arcs": out}


class PopulateArcRequest(BaseModel):
    name: str
    cv_arc_id: int | None = None
    first_issue: int | None = None
    last_issue: int | None = None


def _create_range_arc(series: dict, name: str, first: int, last: int) -> dict:
    """Materialize a single-title discovered arc as a lens over THIS series' issue
    slice (#first-last). Ownership comes straight from the series you're viewing — so
    a re-release like 'TWD Deluxe' resolves correctly, where CV's original volume
    wouldn't."""
    new_id = db.add_series(title=name, publisher=series.get("publisher"),
                           year_began=series.get("year_began"),
                           folder_path=series.get("folder_path"), on_pull_list=False,
                           kind="arc", cv_volume_id=series.get("cv_volume_id"), path=DB_PATH)
    own = {i["number"]: i for i in db.get_issues_for_series(series["id"], DB_PATH)}
    issues, resolved = [], []
    for k, num in enumerate(range(first, last + 1), 1):
        si = own.get(float(num), {})
        issues.append({"reading_order": k, "source_title": series["title"],
                       "number": str(num), "story_title": "", "cv_issue_id": None,
                       "cv_volume_id": series.get("cv_volume_id")})
        resolved.append((k, si.get("komga_book_id"), si.get("owned", 0)))
    db.replace_arc_reading_order(new_id, issues, DB_PATH)
    db.set_arc_ownership(new_id, resolved, DB_PATH)
    return db.get_series_by_id(new_id, DB_PATH)


@router.post("/api/series/{series_id}/arcs/populate")
def populate_arc(series_id: int, req: PopulateArcRequest):
    """Turn a discovered arc into a tracked arc on demand (click-to-populate). For a
    single-title slice we lens it over the viewed series; CV cross-title arcs still
    come in via the ◆ ARC search path."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    from kometa.arc import titles_match
    if req.cv_arc_id:
        ex = db.find_arc_by_cv_id(req.cv_arc_id, DB_PATH)
        if ex:
            return ex
    ex = next((a for a in db.get_all_arcs(DB_PATH) if titles_match(a["title"], req.name)), None)
    if ex:
        return db.get_series_by_id(ex["id"], DB_PATH)
    # CV arc -> precise cross-title order (Brick A-C). Wikipedia range -> single-title
    # lens over the viewed series (ownership-correct for re-releases).
    if req.cv_arc_id:
        return _add_arc(AddSeriesRequest(cv_arc_id=req.cv_arc_id, title=req.name,
                                         publisher_name=s.get("publisher"), on_pull_list=False))
    if req.first_issue and req.last_issue:
        return _create_range_arc(s, req.name, req.first_issue, req.last_issue)
    raise HTTPException(400, "Nothing to populate from")


def _drop_reprints(issues: list[dict]) -> list[dict]:
    """Strip reprint noise from a CV arc reading order. Collected editions and
    foreign reprints surface as a single issue in a one-off volume, while real
    participating runs contribute >=2 (Knightfall: Batman 10, Detective 8, …). Keep
    volumes with >=2 issues (plus unknown-volume rows); fall back to the full list if
    that would empty it (genuine one-shot arcs). Renumbers reading order contiguously."""
    from collections import Counter
    counts = Counter(i["cv_volume_id"] for i in issues if i.get("cv_volume_id"))
    keep_vols = {v for v, c in counts.items() if c >= 2}
    kept = [i for i in issues if not i.get("cv_volume_id") or i["cv_volume_id"] in keep_vols]
    if not kept or len(kept) == len(issues):
        return issues
    for k, i in enumerate(kept, 1):
        i["reading_order"] = k
    return kept


def _vol_title(name: str, year) -> str:
    """Year-qualify a run so it's unambiguous + folders disjoint ('Batman' -> 'Batman
    (1940)'), unless the name already carries the year ('Showcase '93')."""
    if not year:
        return name
    ys = str(year)
    if ys in name or ("'" + ys[-2:]) in name:
        return name
    return f"{name} ({year})"


def _arc_participant_allowed(vol_publisher: str | None, arc_publisher: str | None) -> bool:
    """Publisher gate for auto-tracked arc participants. ComicVine's arc issue
    lists are dirty — they include foreign reprint volumes (Panini's German
    'Batman: Die Neuen Abenteuer - Hush') and magazine promo inserts (Wizard) —
    and their publishers never match the arc's. Only a KNOWN mismatch is gated:
    a missing publisher (CV hiccup, cv unconfigured) passes, so an API blip
    can't silently thin an arc. Cost: a genuine tie-in from a different imprint
    (rare) needs a manual add — the arc's reading order still shows its issues
    either way, there's just no auto-spawned run for them."""
    if not vol_publisher or not arc_publisher:
        return True
    # Strip-everything key (not norm_key): "D.C. Comics" and "DC Comics" must
    # collapse to the same thing, and space-collapsing leaves "d c" != "dc".
    def key(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    return key(vol_publisher) == key(arc_publisher)


def _track_participating(arc_series_id: int) -> dict:
    """Brick B: auto-track the series an arc's issues belong to — one tracked series
    per distinct CV volume, PULL-LIST OFF so broad sweeps skip them (the arc is the
    lens that pulls just its issues). Idempotent via cv_volume_id. Publisher-gated
    (_arc_participant_allowed) so CV's reprint/magazine noise doesn't mint junk
    series. Returns {cv_volume_id: series_id}."""
    cv, komga = _comicvine(), _komga()
    rows = db.get_arc_reading_order(arc_series_id, DB_PATH)
    arc = db.get_series_by_id(arc_series_id, DB_PATH)
    publisher = arc.get("publisher") or "DC Comics"
    vols = {}
    for r in rows:
        vid = r.get("cv_volume_id")
        if vid and vid not in vols:
            vols[vid] = r.get("source_title") or ""
    kall = None  # fetched lazily — skip the whole-library pull when nothing to create
    mapping = {}
    for vid, name in vols.items():
        existing = db.get_series_by_cv_volume(vid, DB_PATH)
        if existing:
            mapping[vid] = existing["id"]
            continue
        # One CV call gets year AND publisher (get_volume_year was fetching the
        # publisher and discarding it) — the gate below is free.
        info = cv.get_volume_info(vid) if cv else {}
        year = info.get("year")
        if not _arc_participant_allowed(info.get("publisher"), publisher):
            logger.info(f"Arc participant skipped (publisher gate): {name!r} is "
                        f"{info.get('publisher')!r}, arc is {publisher!r}")
            continue
        title = _vol_title(name, year)
        kid = None
        if komga:
            try:
                if kall is None:
                    kall = _komga_all_series(komga)
                kid = _best_komga_match(kall, title) or _best_komga_match(kall, name)
            except Exception:
                kid = None
        # That Komga series may already back a tracked run (komga_series_id is UNIQUE —
        # inserting a second 500s). Reuse it: anchor its CV volume to this arc's run and
        # map to it instead of minting a duplicate.
        if kid:
            taken = db.get_series_by_komga_id(kid, DB_PATH)
            if taken:
                if not taken.get("cv_volume_id"):
                    db.set_series_cv_volume(taken["id"], str(vid), DB_PATH)
                mapping[vid] = taken["id"]
                continue
        folder = _resolve_dir(_comics_root(), publisher, title)
        sid = db.add_series(komga_series_id=kid, title=title, publisher=publisher,
                            year_began=year, folder_path=folder, on_pull_list=False,
                            cv_volume_id=str(vid), path=DB_PATH)
        mapping[vid] = sid
        # No try needed: sync_one_guarded catches + logs everything internally.
        sync_one_guarded(db.get_series_by_id(sid, DB_PATH), _sync_one)
        logger.info(f"Arc participating series tracked (pull-off): {title!r} -> series {sid} "
                    f"(komga={'yes' if kid else 'no'})")
    _populate_participating_issues(arc_series_id)
    return mapping


def _populate_participating_issues(arc_series_id: int) -> int:
    """Brick C: stamp each arc issue into its participating run's issue_status, so the
    run's card shows the issues THIS arc needs (owned/missing) and Fulfill can act on
    them. Owned/komga_book_id carry over from the arc's resolved ownership."""
    rows = db.get_arc_reading_order(arc_series_id, DB_PATH)
    vids = {r.get("cv_volume_id") for r in rows if r.get("cv_volume_id")}
    ps_map = {vid: db.get_series_by_cv_volume(vid, DB_PATH) for vid in vids}
    batch = []
    for r in rows:
        ps = ps_map.get(r.get("cv_volume_id"))
        if not ps:
            continue
        try:
            num = float(r.get("number"))
        except (TypeError, ValueError):
            continue
        # Carry the arc issue's CV cover into the run's issue (metron_image) so the
        # scoped run's tiles show art instead of dead boxes — no full-volume pull.
        batch.append((ps["id"], num, r.get("owned", 0), r.get("komga_book_id"), r.get("image_url")))
    db.upsert_issue_status_bulk(batch, DB_PATH)
    return len(batch)


def _stamp_arc_run_issues(run_id: int, arc_rows: list[dict], source_title: str, cv_vid) -> int:
    """Stamp the slice of an arc's reading order that belongs to ONE run into that
    run's issue_status, carrying covers. Match by volume id when the arc has it, else
    by source title. This is what makes a lazily-created run hold ONLY the arc's
    issues (spec: a run created via an arc isn't the whole run)."""
    batch = []
    for r in arc_rows:
        same = (cv_vid and r.get("cv_volume_id") == cv_vid) or \
               (not r.get("cv_volume_id") and r.get("source_title") == source_title)
        if not same:
            continue
        try:
            num = float(r.get("number"))
        except (TypeError, ValueError):
            continue
        batch.append((run_id, num, r.get("owned", 0), r.get("komga_book_id"), r.get("image_url")))
    if batch:
        db.upsert_issue_status_bulk(batch, DB_PATH)
    return len(batch)


def _resolve_or_create_run(arc: dict, row: dict, arc_rows: list[dict]) -> dict:
    """The run an arc issue lives in — resolved if already tracked, else lazily created
    SCOPED (holding just this arc's issues for that run, with covers). Issues live in
    their own runs (the model); opening one is how its run comes into being. No
    download — creation is tracking-only."""
    vid = row.get("cv_volume_id")
    source = row.get("source_title") or ""
    run = (db.get_series_by_cv_volume(vid, DB_PATH) if vid else None) \
        or db.find_series_by_title(source, DB_PATH)
    if run:
        return run
    cv, komga = _comicvine(), _komga()
    year = cv.get_volume_year(vid) if (cv and vid) else None
    title = _vol_title(source, year)
    publisher = arc.get("publisher") or "DC Comics"
    kid = None
    if komga:
        try:
            kall = _komga_all_series(komga)
            kid = _best_komga_match(kall, title) or _best_komga_match(kall, source)
            if kid:
                taken = db.get_series_by_komga_id(kid, DB_PATH)
                if taken:
                    if vid and not taken.get("cv_volume_id"):
                        db.set_series_cv_volume(taken["id"], str(vid), DB_PATH)
                    _stamp_arc_run_issues(taken["id"], arc_rows, source, vid)
                    return db.get_series_by_id(taken["id"], DB_PATH)
        except Exception:
            kid = None
    folder = _resolve_dir(_comics_root(), publisher, title)
    sid = db.add_series(komga_series_id=kid, title=title, publisher=publisher,
                        year_began=year, folder_path=folder, on_pull_list=False,
                        cv_volume_id=str(vid) if vid else None, path=DB_PATH)
    _stamp_arc_run_issues(sid, arc_rows, source, vid)
    # No try needed: sync_one_guarded catches + logs everything internally.
    sync_one_guarded(db.get_series_by_id(sid, DB_PATH), _sync_one)
    out = db.get_series_by_id(sid, DB_PATH)
    out["_created"] = True
    return out


class OpenArcIssueRequest(BaseModel):
    reading_order: int


@router.post("/api/series/{arc_id}/open-issue")
def open_arc_issue(arc_id: int, req: OpenArcIssueRequest):
    """Click an arc issue → resolve (or lazily create) the run it lives in, and return
    where to navigate. Tracking-only: creates the run + stamps the arc's issues, never
    downloads."""
    arc = db.get_series_by_id(arc_id, DB_PATH)
    if not arc or arc.get("kind") != "arc":
        raise HTTPException(404, "Not an arc")
    rows = db.get_arc_reading_order(arc_id, DB_PATH)
    row = next((r for r in rows if r["reading_order"] == req.reading_order), None)
    if not row:
        raise HTTPException(404, "Issue not in arc")
    run = _resolve_or_create_run(arc, row, rows)
    try:
        num = float(row["number"])
    except (TypeError, ValueError):
        num = None
    return {"series_id": run["id"], "number": num, "created": bool(run.get("_created"))}


@router.post("/api/series/{series_id}/fulfill")
def fulfill_arc(series_id: int):
    """Pull just this arc's MISSING issues into their participating runs — singles,
    through the normal cascade, scoped to the arc (not the whole runs)."""
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s or s.get("kind") != "arc":
        raise HTTPException(404, "Not a story arc")
    _track_participating(series_id)  # ensure runs exist + issues stamped (idempotent/fast)
    rows = db.get_arc_reading_order(series_id, DB_PATH)
    vids = {r.get("cv_volume_id") for r in rows if r.get("cv_volume_id")}
    ps_map = {vid: db.get_series_by_cv_volume(vid, DB_PATH) for vid in vids}
    pairs = []
    for r in rows:
        if r.get("owned"):
            continue
        ps = ps_map.get(r.get("cv_volume_id"))
        if not ps:
            continue
        try:
            pairs.append((ps["id"], float(r.get("number"))))
        except (TypeError, ValueError):
            continue
    db.queue_issues_bulk(pairs, DB_PATH)
    if pairs:
        threading.Thread(target=_process_queue, daemon=True).start()
    return {"queued": len(pairs), "total": len(rows), "owned": sum(1 for r in rows if r.get("owned"))}


def _add_arc(req: AddSeriesRequest):
    """Add a story arc: a kind='arc' tracked_series whose cross-title reading order
    is populated from ComicVine. Reuses folder/queue/Komga machinery; the arc's
    issues span titles so they live in arc_issues, not issue_status."""
    # Already tracking this arc? Open it instead of duplicating.
    existing = db.find_arc_by_cv_id(req.cv_arc_id, DB_PATH)
    if existing:
        return existing
    cv = _comicvine()
    title = req.title or ""
    publisher = req.publisher_name or "DC Comics"
    folder = req.folder_path or _resolve_dir(_comics_root(), publisher or "Unknown", title)
    new_id = db.add_series(
        title=title, publisher=publisher, year_began=req.year_began,
        folder_path=folder, on_pull_list=req.on_pull_list,
        kind="arc", cv_arc_id=str(req.cv_arc_id), path=DB_PATH,
    )
    added = db.get_series_by_id(new_id, DB_PATH)

    def _bg():
        if not cv:
            return
        try:
            base = cv.get_arc_issues(req.cv_arc_id)
            # Enrich with authoritative CV meta: the REAL issue number (fixes slug
            # mis-parses like Showcase '93 → #7/#8) and the exact volume (so each
            # issue routes to the right run — Batman 1940, not 2016). One batch call.
            meta = cv.get_issues_meta([r["cv_issue_id"] for r in base])
            issues = []
            for r in base:
                m = meta.get(str(r["cv_issue_id"]), {})
                num = m.get("number") or r["number"]
                issues.append({
                    "reading_order": r["order"],
                    "source_title": m.get("volume_name") or r["series"],
                    "number": str(num) if num is not None else r["number"],
                    "story_title": r["title"],
                    "cv_issue_id": str(r["cv_issue_id"]),
                    "cv_volume_id": str(m["volume_id"]) if m.get("volume_id") else None,
                    "image_url": m.get("image_url"),
                })
            kept = _drop_reprints(issues)
            if len(kept) < len(issues):
                logger.info(f"Arc {title!r}: dropped {len(issues) - len(kept)} reprint/edition issues")
            issues = kept
            db.replace_arc_reading_order(new_id, issues, DB_PATH)
            logger.info(f"Arc {title!r}: populated {len(issues)} reading-order issues from CV")
            # Resolve cross-title ownership against Komga so the arc page shows real
            # owned counts from the start, not 0/N.
            try:
                r = _resolve_arc_ownership(new_id)
                logger.info(f"Arc {title!r}: resolved {r['owned']}/{r['total']} owned in Komga")
            except Exception as e:
                logger.warning(f"Arc ownership resolve failed for {title!r}: {e}")
            # Brick B: auto-track the participating runs (pull-list OFF) so the arc's
            # issues have a home and the library reflects them — without sweeping them.
            try:
                m = _track_participating(new_id)
                logger.info(f"Arc {title!r}: tracking {len(m)} participating series (pull-off)")
            except Exception as e:
                logger.warning(f"Arc participating-track failed for {title!r}: {e}")
        except Exception as e:
            logger.warning(f"Arc populate failed for {title!r}: {e}")
        if req.on_pull_list:
            # The collected edition belongs to the arc's MAIN series, not the arc
            # (lens model): route the trade grab to the main series so the file lands
            # in ITS folder + Trades tab. Falls back to the arc only if the main
            # series isn't tracked yet. Cascade (GetComics→Usenet→Torrent) unchanged.
            from kometa.arc import main_series_title
            ro = db.get_arc_reading_order(new_id, DB_PATH)
            main_title = main_series_title(ro)
            main = db.find_series_by_title(main_title, DB_PATH) if main_title else None
            target_id = main["id"] if main else new_id
            db.queue_trade(target_id, f"arc-{req.cv_arc_id}", title, path=DB_PATH)
            threading.Thread(target=_process_queue, daemon=True).start()
            logger.info(f"Arc {title!r}: queued trade grab → "
                        f"{'main series ' + repr(main_title) if main else 'arc folder (main untracked)'}")

    threading.Thread(target=_bg, daemon=True).start()
    return added
