"""Per-series sync — reconcile one tracked series against Komga (ownership +
book IDs) and the metadata sources, then upsert the merged issue list.

Ownership is what's on disk; the Komga book map only supplies book IDs for
thumbnails. LOCG is authoritative for the issue list (covers land in the
legacy-named metron_image column); CV fills arc/storyline runs and upcoming
solicitations.
"""
import os
import re
import time
import logging
import threading

from kometa.sources import (
    komga as _komga, locg as _locg,
)
from kometa.naming import (
    scan_folder_numbers as _scan_folder_numbers, parse_issue_number as _parse_issue_number,
    scan_folder_volumes as _scan_folder_volumes, parse_volume_number as _parse_volume_number,
    norm_key as _norm_name,
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

# Concurrency guards. SEVEN entry points can reach sync_one (manual per-series,
# sync-all, the cron, the startup catch-up, add_series, and two arc-path inline
# calls) and SQLite has exactly ONE WAL writer slot — overlap them and the loser
# eats "database is locked". Per-series locks collapse duplicate syncs of the
# same series (sync_one is idempotent, so dropping the second is correct, not
# lossy); full_sync_lock keeps whole-catalog sweeps from stacking on top of
# each other (deploy-near-cron-hour spawns the catch-up AND the cron fire).
_sync_locks: dict = {}
_sync_locks_guard = threading.Lock()
full_sync_lock = threading.Lock()


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
    if series.get("kind") == "arc":
        # Arcs are populated from ComicVine on add and carry no single-title
        # issue_status, so the normal sync (Komga link, LOCG issues, trades)
        # doesn't apply. Phase E adds arc-specific sync (ownership + trades).
        return
    komga = _komga()

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
    komga_books: list[dict] | None = None  # raw list, reused by enrich_trades below
    if series.get("komga_series_id") and komga:
        try:
            komga_books = komga.get_books(series["komga_series_id"])
            for b in komga_books:
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

    # --- Build issue map from LoCG (best for upcoming solicitations) ---
    # Auth buys series-id lookup; but if we already have a locg_series_id (e.g.
    # the series was added via the LOCG wizard) we can pull its issues with no
    # login at all. That anon path is what makes keyless onboarding actually work.
    issue_map: dict[float, dict] = {}
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
                enrich_trades(series, trades, books=komga_books)
                db.set_trades(series["id"], trades, DB_PATH)
            except Exception as e:
                logger.warning(f"Trades cache failed for '{series['title']}': {e}")

    # NB: a storyline-followed run (only a cv_volume_id, no LOCG/Komga) is
    # SCOPED to its arcs — it does NOT pull the full CV volume. Its issues are stamped
    # in by arc participation (_populate_participating_issues), carrying their covers.
    # So the loops above legitimately leave issue_map empty here; that's fine.

    # --- Upsert merged issue list (one transaction — not a connection per issue) ---
    db.upsert_issue_status_many(
        [(series["id"], num, data["store_date"], num in owned_numbers,
          book_map.get(num), data.get("image"), data.get("locg_issue_id"))
         for num, data in issue_map.items()],
        path=DB_PATH,
    )

    # Folder is the source of truth for ownership — reconcile from disk (this also
    # CREATES issues for files that aren't in the metadata list, e.g. when LOCG
    # is unavailable), so ownership never depends on the network. The folder was
    # already listed above — hand the numbers over instead of scanning it twice.
    rescan_owned(series, owned_numbers=owned_numbers if folder and os.path.isdir(folder) else None)

    # Stamp Komga book ids onto the (now reconciled) issues. The upsert above only
    # reached issues that came from a metadata source; a folder-only series (no
    # CV/LOCG — e.g. a Noir Edition) builds its issue list purely from disk via
    # rescan_owned, which knows nothing of book_map. Without this its owned issues get
    # no komga_book_id → no thumbnail, no read link. UPDATE is a no-op for any book
    # number that has no matching issue row.
    db.set_komga_book_ids_bulk(series["id"], book_map, DB_PATH)

    db.mark_synced(series["id"], DB_PATH)


def sync_one_guarded(series: dict, fn=None) -> bool:
    """sync_one behind a per-series lock. If a sync of THIS series is already
    in flight, return False and walk away — the running one will produce the
    same result, so waiting in line just doubles the work. Also the ONLY place
    sync failures get logged for the thread-target callers: a bare Thread
    swallows its exception and dies silently, which is how syncs used to
    vanish without a trace. Returns True if the sync ran (even if it failed).

    `fn` is the seam: main passes its own late-bound `_sync_one` so tests can
    monkeypatch main._sync_one and neutralize background syncs — otherwise a
    bg thread here would write to the REAL database mid-test. Defaults to the
    genuine sync_one."""
    with _sync_locks_guard:
        lock = _sync_locks.setdefault(series["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        logger.info(f"Sync already running for {series.get('title')!r} (id={series['id']}) — skipping")
        return False
    try:
        (fn or sync_one)(series)
    except Exception:
        logger.exception(f"Sync failed for {series.get('title')!r} (id={series['id']})")
    finally:
        lock.release()
    return True


def _scan_folder_edition_names(folder_path: str) -> set[str]:
    """Normalized stems of NON-volume-numbered comic files on disk — the ownership
    key for no-volume editions (OGNs, Compendiums, year HCs) that carry no volume
    number for scan_folder_volumes to see."""
    exts = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
    names = set()
    try:
        for name in os.listdir(folder_path):
            stem, ext = os.path.splitext(name)
            if ext.lower() in exts and _parse_volume_number(name) is None:
                names.add(_norm_name(stem))
    except Exception:
        pass
    return names


def enrich_trades(series: dict, trades: list[dict], books: list[dict] | None = None) -> list[dict]:
    """Stamp the two stored facts onto each trade: `owned` (file in the folder) and
    `komga_book_id` (the matching Komga book, for the read link). Computed at sync
    time and cached so request handlers never fold-scan. The two are independent —
    the folder answers 'do I have it', Komga answers 'can I read it'; Komga is never
    an ownership source.

    books: pass the series' Komga book list if already fetched (sync_one pulls it
    for the issue book map) to avoid a second full paginated get_books."""
    folder = series.get("folder_path")
    owned_vols = _scan_folder_volumes(folder) if folder else set()
    owned_names = _scan_folder_edition_names(folder) if folder else set()

    kbook_by_vol, kbook_by_name = {}, {}
    if books is None:
        komga = _komga()
        if komga and series.get("komga_series_id"):
            try:
                books = komga.get_books(series["komga_series_id"])
            except Exception as e:
                logger.warning(f"Komga trade-book map failed for '{series.get('title')}': {e}")
    for b in books or []:
        name = b.get("name", "")
        v = _parse_volume_number(name)
        if v is not None:
            kbook_by_vol[v] = b["id"]
        kbook_by_name[_norm_name(name)] = b["id"]

    for t in trades:
        if t.get("vol") is not None:
            t["owned"] = t["vol"] in owned_vols
            t["komga_book_id"] = kbook_by_vol.get(t["vol"])
        elif t.get("vol_range"):
            lo, hi = t["vol_range"]
            t["owned"] = all(v in owned_vols for v in range(lo, hi + 1))
            t["komga_book_id"] = None  # a range spans multiple books — no single link
        else:
            # No volume number — an OGN / one-shot ("Gigs TP"). It can't match by
            # volume, so key off the edition title: the file we place is named for it
            # (_trade_fallback_name → edition_title), and Komga's book carries the same
            # name. Without this an owned OGN reads forever-missing on the Trades tab.
            key = _norm_name(t.get("title", ""))
            t["owned"] = bool(key) and key in owned_names
            t["komga_book_id"] = kbook_by_name.get(key)
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


def rescan_owned(series: dict, owned_numbers: set | None = None) -> dict:
    """Folder is the source of truth for ownership. Scan it and reconcile owned:
    CREATE an owned issue for each file not yet tracked, mark found ones owned, and
    clear ones whose file is gone. Pure disk — no CV/LOCG — so it works even
    when those are blocked. Returns {scanned, owned}.

    owned_numbers: pass a set already derived from this folder (sync_one scans it
    for the metadata merge anyway) to skip re-listing the directory."""
    folder = series.get("folder_path")
    if not folder or not os.path.isdir(folder):
        return {"scanned": False, "owned": 0}
    if owned_numbers is None:
        owned_numbers = _scan_folder_numbers(folder, series.get("title", ""))
    existing = {i["number"]: i for i in db.get_issues_for_series(series["id"], DB_PATH)}
    db.set_owned_bulk(
        series["id"],
        [num for num in owned_numbers if num in existing and not existing[num]["owned"]],
        True, DB_PATH,
    )
    db.upsert_issue_status_many(
        [(series["id"], num, None, True, None, None, None)  # straight from disk
         for num in owned_numbers if num not in existing],
        path=DB_PATH,
    )
    db.set_owned_bulk(
        series["id"],
        [num for num, iss in existing.items() if num not in owned_numbers and iss["owned"]],
        False, DB_PATH,
    )
    return {"scanned": True, "owned": len(owned_numbers)}
