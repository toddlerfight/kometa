"""Per-series sync — reconcile one tracked series against Komga (ownership +
book IDs) and the metadata sources (Metron primary, ComicVine and LOCG as
supplements), then upsert the merged issue list.

Ownership is what's on disk; the Komga book map only supplies book IDs for
thumbnails. Metron is authoritative for the issue list; CV/LOCG fill gaps and
add upcoming solicitations.
"""
import os
import re
import time
import logging

from kometa.sources import (
    komga as _komga, metron as _metron, comicvine as _comicvine, locg as _locg,
)
from kometa.naming import (
    scan_folder_numbers as _scan_folder_numbers, parse_issue_number as _parse_issue_number,
    scan_folder_volumes as _scan_folder_volumes, parse_volume_number as _parse_volume_number,
)
from kometa.locg_client import get_issues_anon, get_trades_anon, select_editions
import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH

# Full Komga library, cached briefly so a 47-series sync_all loop pulls it ONCE
# instead of hammering Komga per series. TTL is short — a sync run is the only
# place this gets read in bursts.
_KOMGA_ALL_CACHE: dict = {"ts": 0.0, "data": None}
_KOMGA_ALL_TTL = 120  # seconds


def _komga_all_series(komga):
    """The whole Komga library (cached). Returns a list; [] on failure."""
    now = time.time()
    if _KOMGA_ALL_CACHE["data"] is None or now - _KOMGA_ALL_CACHE["ts"] > _KOMGA_ALL_TTL:
        try:
            _KOMGA_ALL_CACHE["data"] = komga.get_all_series()
            _KOMGA_ALL_CACHE["ts"] = now
        except Exception as e:
            logger.warning(f"Komga get_all_series failed: {e}")
            return _KOMGA_ALL_CACHE["data"] or []
    return _KOMGA_ALL_CACHE["data"] or []


def _best_komga_match(candidates, title):
    """Pick the Komga series matching title — only when there's exactly ONE exact
    (normalised) title match, so ambiguous names (e.g. multiple Batman runs) don't
    mis-link. Match is punctuation-insensitive (strips everything but a-z0-9), so
    Kometa's 'Batman: Gargoyle of Gotham' links to Komga's 'Batman - Gargoyle of
    Gotham'. Feed it the FULL library (not a Komga /search result), since Komga's
    search itself chokes on the punctuation we're trying to ignore. Returns id or None."""
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    tn = norm(title)
    if not tn:
        return None
    exact = [r.get("id") for r in (candidates or [])
             if norm((r.get("metadata") or {}).get("title") or r.get("name") or "") == tn]
    return exact[0] if len(exact) == 1 else None


def _best_komga_match_by_path(candidates, folder_path):
    """Match a series to its Komga counterpart by FOLDER PATH (Komga's series.url).
    This is the unambiguous join key — same /comics mount on both sides — and it's
    the ONLY thing that can disambiguate cases title-matching can't: three bare
    'Batman' series in Komga (different runs) all normalise identically, but their
    folder urls are distinct. Returns the id on a single exact path match, else None."""
    if not folder_path:
        return None
    hits = [r.get("id") for r in (candidates or []) if r.get("url") == folder_path]
    return hits[0] if len(hits) == 1 else None


def sync_one(series: dict):
    komga = _komga()
    metron = _metron()

    # Auto-link to a Komga series when connected but unlinked. Folder PATH first
    # (unambiguous — disambiguates same-titled runs like the Batman 2016/2025), then
    # fall back to normalised title for series whose folder Komga hasn't got.
    if not series.get("komga_series_id") and komga:
        try:
            all_komga = _komga_all_series(komga)
            kid = (_best_komga_match_by_path(all_komga, series.get("folder_path"))
                   or _best_komga_match(all_komga, series["title"]))
            if kid and db.set_komga_series_id(series["id"], kid, DB_PATH):
                series = dict(series, komga_series_id=kid)
        except Exception as e:
            logger.warning(f"Komga auto-link failed for {series.get('title')!r}: {e}")

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

    # Komga book map — issue number -> book id, for stamping komga_book_id (thumbnails
    # + reader links). The FILENAME is the source of truth: Komga's numberSort is an
    # unreliable running counter that TPBs/specials/dupes shift out of alignment (e.g.
    # "Monstress #062" gets numberSort 83), so a numberSort-keyed map silently drops
    # real issues. Parse the filename first; fall back to numberSort only when the name
    # has no parseable issue number. On a clash, a filename-derived key always wins.
    book_map: dict[float, str] = {}
    book_src: dict[float, str] = {}  # 'name' (authoritative) vs 'sort' (fallback)
    if series.get("komga_series_id") and komga:
        try:
            for b in komga.get_books(series["komga_series_id"]):
                if b.get("media", {}).get("status") == "ERROR":
                    continue
                fn_num = _parse_issue_number(b.get("name", ""), series.get("title", ""))
                if fn_num is not None:
                    key, src = fn_num, "name"
                    # Komga's own number for this book is unreliable — push our
                    # filename-derived number back (locked) so Komga's labels AND
                    # ordering (it sorts by numberSort) match reality. Only when it
                    # disagrees, so we're not re-writing on every sync. Best-effort.
                    if komga and b["metadata"].get("numberSort") != fn_num:
                        num_str = str(int(fn_num)) if fn_num == int(fn_num) else str(fn_num)
                        try:
                            komga.set_book_number(b["id"], num_str, fn_num)
                        except Exception as e:
                            logger.warning(f"Komga renumber failed for book {b['id']}: {e}")
                else:
                    n = b["metadata"].get("numberSort")
                    if n is None:
                        continue
                    key, src = float(n), "sort"
                # A filename-derived ('name') key always wins. So only overwrite an
                # existing entry when the incumbent is a 'sort' fallback AND the new one
                # is authoritative; otherwise keep what's already there (authoritative
                # incumbent stays, fallback-vs-fallback keeps the first seen).
                if key in book_map and not (book_src[key] == "sort" and src == "name"):
                    continue
                book_map[key] = b["id"]
                book_src[key] = src
        except Exception:
            pass

    # Ownership = what's on disk, FULL STOP. book_map exists only to stamp
    # komga_book_id for thumbnails — it is NOT an ownership source. No folder, or
    # a folder that isn't there yet? Then nothing is owned until a real file lands
    # (rescan_owned, below, is the sole authority and re-derives purely from disk).
    # Komga's book list does NOT get a vote here — that's the rule.
    folder = series.get("folder_path")
    owned_numbers = (
        _scan_folder_numbers(folder, series.get("title", ""))
        if folder and os.path.isdir(folder)
        else set()
    )

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
    # Auth buys series-id lookup; but if we already have a locg_series_id (e.g.
    # the series was added via the LOCG wizard) we can pull its issues with no
    # login at all. That anon path is what makes keyless onboarding actually work.
    locg = _locg()
    locg_id = series.get("locg_series_id")
    if locg or locg_id:
        try:
            if locg and not locg_id:
                locg_id = locg.find_series_id(series["title"], series.get("year_began"))
                if locg_id:
                    db.set_locg_series_id(series["id"], locg_id, DB_PATH)
                    series = dict(series, locg_series_id=locg_id)
            if locg_id:
                locg_issues = locg.get_issues(locg_id) if locg else get_issues_anon(locg_id)
                for li in locg_issues:
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

        # Cache available collected editions while we're here — same anon LOCG
        # path, so the Trades tab reads instantly instead of searching live every
        # open. Enrich with the two stored facts (owned from the folder, komga_book_id
        # from Komga) so reads never fold-scan. Best-effort: a trades hiccup must
        # never fail an issue sync.
        if locg_id:
            try:
                trades = select_editions(get_trades_anon(locg_id))
                enrich_trades(series, trades)
                db.set_trades(series["id"], trades, DB_PATH)
            except Exception as e:
                logger.warning(f"Trades cache failed for '{series['title']}': {e}")

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

    # Folder is the source of truth for ownership — reconcile from disk (this also
    # CREATES issues for files that aren't in the metadata list, e.g. when LOCG/
    # Metron is unavailable), so ownership never depends on the network.
    rescan_owned(series)

    # Stamp Komga book ids onto the (now reconciled) issues. The upsert loop above
    # only reached issues that came from a metadata source; a folder-only series (no
    # Metron/CV/LOCG — e.g. a Noir Edition) builds its issue list purely from disk via
    # rescan_owned, which knows nothing of book_map. Without this its owned issues get
    # no komga_book_id → no thumbnail, no read link. UPDATE is a no-op for any book
    # number that has no matching issue row.
    for num, bid in book_map.items():
        db.set_komga_book_id(series["id"], num, bid, DB_PATH)

    db.mark_synced(series["id"], DB_PATH)


def enrich_trades(series: dict, trades: list[dict]) -> list[dict]:
    """Stamp the two stored facts onto each trade: `owned` (file in the folder) and
    `komga_book_id` (the matching Komga book, for the read link). Computed at sync
    time and cached so request handlers never fold-scan. The two are independent —
    the folder answers 'do I have it', Komga answers 'can I read it'; Komga is never
    an ownership source."""
    folder = series.get("folder_path")
    owned_vols = _scan_folder_volumes(folder) if folder else set()

    kbook_by_vol = {}
    komga = _komga()
    if komga and series.get("komga_series_id"):
        try:
            for b in komga.get_books(series["komga_series_id"]):
                v = _parse_volume_number(b.get("name", ""))
                if v is not None:
                    kbook_by_vol[v] = b["id"]
        except Exception as e:
            logger.warning(f"Komga trade-book map failed for '{series.get('title')}': {e}")

    for t in trades:
        if t.get("vol") is not None:
            t["owned"] = t["vol"] in owned_vols
            t["komga_book_id"] = kbook_by_vol.get(t["vol"])
        elif t.get("vol_range"):
            lo, hi = t["vol_range"]
            t["owned"] = all(v in owned_vols for v in range(lo, hi + 1))
            t["komga_book_id"] = None  # a range spans multiple books — no single link
        else:
            t["owned"] = False
            t["komga_book_id"] = None
    return trades


def refresh_trades_owned(series_id: int) -> None:
    """Re-stamp owned/komga on a series' cached trades without re-hitting LOCG —
    used right after a trade download so the tile flips to owned immediately
    instead of waiting for the next sync."""
    cached = db.get_trades(series_id, DB_PATH)
    if not cached:
        return
    series = db.get_series_by_id(series_id, DB_PATH)
    if not series:
        return
    enrich_trades(series, cached["trades"])
    db.set_trades(series_id, cached["trades"], DB_PATH)


def rescan_owned(series: dict) -> dict:
    """Folder is the source of truth for ownership. Scan it and reconcile owned:
    CREATE an owned issue for each file not yet tracked, mark found ones owned, and
    clear ones whose file is gone. Pure disk — no Metron/CV/LOCG — so it works even
    when those are blocked. Returns {scanned, owned}."""
    folder = series.get("folder_path")
    if not folder or not os.path.isdir(folder):
        return {"scanned": False, "owned": 0}
    owned_numbers = _scan_folder_numbers(folder, series.get("title", ""))
    existing = {i["number"]: i for i in db.get_issues_for_series(series["id"], DB_PATH)}
    for num in owned_numbers:
        if num in existing:
            if not existing[num]["owned"]:
                db.set_owned(series["id"], num, True, DB_PATH)
        else:
            db.upsert_issue_status(series["id"], num, None, owned=True, path=DB_PATH)  # straight from disk
    for num, iss in existing.items():
        if num not in owned_numbers and iss["owned"]:
            db.set_owned(series["id"], num, False, DB_PATH)
    return {"scanned": True, "owned": len(owned_numbers)}
