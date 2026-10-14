"""Per-series sync — reconcile one tracked series against its folder on disk
(ownership) and the metadata sources, then upsert the merged issue list.

Ownership is what's on disk, and nothing else. Komga used to be asked for a
series link and a folder; it's retired (2026-10-12) and sync never talks to
it. LOCG is authoritative for the issue list (covers land in the
legacy-named metron_image column); CV fills arc/storyline runs and upcoming
solicitations.
"""
import os
import re
import logging
import threading

from kometa.naming import (
    scan_folder_numbers as _scan_folder_numbers, parse_issue_number as _parse_issue_number,
    scan_folder_volume_entries as _scan_folder_volume_entries,
    parse_volume_number as _parse_volume_number,
    edition_keywords as _edition_keywords,
    norm_key as _norm_name, OWNED_EXTS,
)
from kometa.locg_client import (
    get_issues_anon, get_trades_anon, select_editions, find_series_id_anon,
)
import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH

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



# --- How much LOCG a sync is allowed (2026-10-08) ------------------------------
# Every sync used to refetch the full issue list AND the trades list for every
# series — ~800 LOCG calls a day, nearly all returning what we already had.
# What actually changes on LOCG: new solicitations (monthly, ~3 months out),
# shifting dates and fresh covers for UPCOMING issues. A finished run doesn't.
ACTIVE_WINDOW_DAYS = 180     # a release this recent (or any upcoming) = still moving
DORMANT_REFRESH_DAYS = 30    # a finished run's list: monthly at most
TRADES_REFRESH_DAYS = 30


def _issues_due(series: dict, stamp_key: str = "locg_fetched_at") -> bool:
    """Should this (non-forced) sync refetch an issue list (LOCG or Metron)?"""
    from datetime import date, datetime, timedelta, timezone
    fetched = series.get(stamp_key)
    if not fetched:
        return True                                   # never fetched (or relinked)
    recent = str(date.today() - timedelta(days=ACTIVE_WINDOW_DAYS))
    if any(i.get("store_date") and i["store_date"] >= recent
           for i in db.get_issues_for_series(series["id"], DB_PATH)):
        return True                                   # ongoing: cadence is the caller's
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DORMANT_REFRESH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    return fetched < cutoff                           # finished run: monthly at most


LOCG_WITH_METRON_DAYS = 7   # Metron has the list; LOCG only adds far-ahead solicits


def _locg_issues_due(series: dict, have_metron: bool = False) -> bool:
    if have_metron:
        # Metron covers the issue list. LOCG's one extra — solicitations months
        # ahead — only matters for series you're pulling, and weekly is plenty.
        if not series.get("on_pull_list"):
            return False
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=LOCG_WITH_METRON_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        return (series.get("locg_fetched_at") or "") < cutoff
    return _issues_due(series, "locg_fetched_at")


def _trades_due(series: dict) -> bool:
    cached = db.get_trades(series["id"], DB_PATH)
    return not cached or cached["age"] > TRADES_REFRESH_DAYS * 86400


def sync_one(series: dict, force: bool = False):
    """force = you asked (pull-to-refresh, Match now, a new LOCG link): fetch
    everything. Otherwise LOCG is only asked for what can have changed."""
    if series.get("kind") == "arc":
        # Arcs are populated from ComicVine on add and carry no single-title
        # issue_status, so the normal sync (folder, LOCG issues, trades)
        # doesn't apply. Phase E adds arc-specific sync (ownership + trades).
        return
    # No Komga (retired 2026-10-12): no series link, no book map, no folder
    # lookup — a series without a folder_path simply owns nothing until it gets
    # one (add_series always derives one, so that's the odd old row).
    # Ownership = what's on disk, FULL STOP. No folder, or a folder that isn't
    # there yet? Then nothing is owned until a real file lands (rescan_owned,
    # below, is the sole authority and re-derives purely from disk).
    folder = series.get("folder_path")
    owned_numbers = (
        _scan_folder_numbers(folder, series.get("title", ""))
        if folder and os.path.isdir(folder)
        else set()
    )

    # --- Build issue map from LoCG (best for upcoming solicitations) ---
    # All keyless: series added via the wizard already carry a locg_series_id;
    # one without a linked id (e.g. a shelf import) gets resolved by title here.
    # LOCG never needed a login — the search/issue endpoints ignore auth.
    issue_map: dict[float, dict] = {}
    locg_id = series.get("locg_series_id")
    try:
        # A shelf-imported series the importer couldn't match confidently is
        # waiting for YOU to pick its run — a title guess here is exactly the
        # silent wrong-run match that state exists to prevent.
        # Title-guessing is only for series added the old way (match_status NULL).
        # Shelf-imported ones are matched by the confident matcher — never guessed.
        if not locg_id and series.get("title") and series.get("match_status") is None:
            locg_id = find_series_id_anon(series["title"], series.get("year_began"))
            if locg_id:
                db.set_locg_series_id(series["id"], locg_id, DB_PATH)
                series = dict(series, locg_series_id=locg_id)
        # Metron first — the API built for this. Its list is the base; LOCG adds
        # what Metron doesn't have yet (far-ahead solicitations) on top.
        have_metron = False
        mid = series.get("metron_series_id")
        if mid:
            from kometa import metron_client
            if metron_client.configured() and (force or _issues_due(series, "metron_fetched_at")):
                try:
                    # the primary run, then any extra runs this one spans (db.metron_series_ids);
                    # a number the primary already has is never overwritten by an extra
                    for run in db.metron_series_ids(series):
                        for mi in metron_client.series_issues(run):
                            issue_map.setdefault(mi["number"], {"store_date": mi["store_date"], "image": mi["image"],
                                                                "locg_issue_id": None, "metron_issue_id": mi.get("metron_issue_id")})
                    if issue_map:
                        from datetime import datetime, timezone
                        db.set_metron_fetched(series["id"], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), DB_PATH)
                    if series.get("metron_type") is None or series.get("metron_status_at") is None:
                        # once per series: what KIND of run this is (one-shot, limited,
                        # trade…) — kometa/singles.py reads it to tell a stub from a book —
                        # and, off the same page, whether it's still running (runstatus.py)
                        try:
                            d = metron_client.series_detail(mid)
                            if series.get("metron_type") is None:
                                db.set_metron_type(series["id"], d.get("type"), DB_PATH)
                            db.set_metron_status(series["id"], d.get("status"), d.get("year_end"), DB_PATH)
                        except metron_client.MetronUnavailable:
                            pass
                except metron_client.MetronUnavailable as e:
                    logger.info(f"Metron unavailable for {series['title']!r}: {e}")
            have_metron = bool(series.get("metron_fetched_at")) or bool(issue_map)
        fetch_issues = bool(locg_id) and (force or _locg_issues_due(series, have_metron))
        if fetch_issues:
            fetched_any = False
            for li in get_issues_anon(locg_id):
                fetched_any = True
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
            if fetched_any:
                from datetime import datetime, timezone
                db.set_locg_fetched(series["id"], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), DB_PATH)
    except Exception as e:
        logger.warning(f"LoCG supplement failed for '{series['title']}': {e}")

    # Cache available collected editions while we're here — same anon LOCG
    # path, so the Trades tab reads instantly instead of searching live every
    # open. Enrich with the stored facts (owned + the file, from the folder) so
    # reads never fold-scan. Best-effort: a trades hiccup must
    # never fail an issue sync.
    if (locg_id or series.get("metron_series_id")) and (force or _trades_due(series)):
        try:
            from kometa.record import fill_trades
            fill_trades(series, force=force, path=DB_PATH)
        except Exception as e:
            logger.warning(f"Trades cache failed for '{series['title']}': {e}")

    # NB: a storyline-followed run (only a cv_volume_id, no LOCG/Metron) is
    # SCOPED to its arcs — it does NOT pull the full CV volume. Its issues are stamped
    # in by arc participation (_populate_participating_issues), carrying their covers.
    # So the loops above legitimately leave issue_map empty here; that's fine.

    # --- Upsert merged issue list (one transaction — not a connection per issue) ---
    db.upsert_issue_status_many(
        [(series["id"], num, data["store_date"], num in owned_numbers,
          None, data.get("image"), data.get("locg_issue_id"), data.get("metron_issue_id"))
         for num, data in issue_map.items()],
        path=DB_PATH,
    )

    # Folder is the source of truth for ownership — reconcile from disk (this also
    # CREATES issues for files that aren't in the metadata list, e.g. when LOCG
    # is unavailable), so ownership never depends on the network. The folder was
    # already listed above — hand the numbers over instead of scanning it twice.
    rescan_owned(series, owned_numbers=owned_numbers if folder and os.path.isdir(folder) else None)

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


def _scan_folder_edition_names(folder_path: str, listing: list[str] | None = None) -> dict[str, str]:
    """Normalized stem → file name of NON-volume-numbered comic files on disk —
    the ownership key for no-volume editions (OGNs, Compendiums, year HCs) that
    carry no volume number for scan_folder_volumes to see."""
    names: dict[str, str] = {}
    try:
        for name in (listing if listing is not None else os.listdir(folder_path)):
            stem, ext = os.path.splitext(name)
            if ext.lower() in OWNED_EXTS and _parse_volume_number(name) is None:
                # the year a file carries ('DIE HC (2022)') is not part of the edition's name
                names.setdefault(_norm_name(re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", stem)), name)
    except Exception:
        pass
    return names


def enrich_trades(series: dict, trades: list[dict],
                  landed: dict[str, str] | None = None) -> list[dict]:
    """Stamp the stored facts onto each trade: `owned` (an edition's file is in the
    folder) and `file` (which one). Computed at sync time and cached so request
    handlers never fold-scan. (A third, `komga_book_id`, was Komga's read link;
    the reader opens the file by name, and Komga is retired.)

    landed: {lowercased file name: edition id} for the trades Kometa downloaded
    itself (db.landed_trade_files) — the surest word on which edition a file IS."""
    folder = series.get("folder_path")
    # ONE directory listing (an NFS readdir each) feeds all three views below
    listing: list[str] = []
    if folder:
        try:
            listing = os.listdir(folder)
        except OSError:
            listing = []
    vol_entries = _scan_folder_volume_entries(folder, listing) if folder else []
    owned_names = _scan_folder_edition_names(folder, listing) if folder else {}

    # Volume ownership is EDITION-AWARE: the trade's special-edition words
    # (absolute/omnibus/…) must equal the file's, or a plain Vol 1 TPB on disk
    # stamps "Absolute Vol. 1 HC" owned and the sweep never fetches the real one.
    def _vol_owned(vol, kws):
        return any(v == vol and _edition_keywords(name) == kws for v, name in vol_entries)

    # scan_folder_volume_entries lowercases its names for the keyword compare;
    # the file we stamp must be the REAL name, or the shelf page's dedupe and
    # the reader's book lookup (both exact-string) never find it.
    real_names = {n.lower(): n for n in listing}

    def _vol_files(vol, kws):
        return [real_names.get(name, name) for v, name in vol_entries if v == vol and _edition_keywords(name) == kws]

    vol_bids: list[tuple] = []        # (fit, order, trade, file): settled below, one file per edition
    for i, t in enumerate(trades):
        kws = _edition_keywords(t.get("title", ""))
        # `file`: the file on disk this edition IS — the series page keeps it off
        # 'Also on the shelf' and the Trades tab reads it
        t["file"] = None
        if t.get("vol") is not None:
            t["owned"] = False
            for f in _vol_files(t["vol"], kws):
                vol_bids.append((_trade_file_fit(t, f, landed), -i, t, f))
        elif t.get("vol_range"):
            lo, hi = t["vol_range"]
            t["owned"] = all(_vol_owned(v, kws) for v in range(lo, hi + 1))
        else:
            # No volume number — an OGN / one-shot ("Gigs TP"). It can't match by
            # volume, so key off the edition title: the file we place is named for it
            # (_trade_fallback_name → edition_title). Without this an owned OGN reads forever-missing on the Trades tab.
            key = _norm_name(t.get("title", ""))
            t["owned"] = bool(key) and key in owned_names
            t["file"] = owned_names.get(key) if t["owned"] else None
    # Volume ownership, settled. (vol, edition words) alone let 'Avengers Academy:
    # The Complete Collection Vol. 2 TP' AND 'Vol. 2: Will We Use This in the Real
    # World? HC' both grab the one Vol 2 file — two editions "owned", one book on
    # the disk, and the HC's issues covered by a book that isn't it. Best fit picks
    # first (the edition Kometa downloaded the file AS, then the title that looks
    # most like the file's name, then TP/HC agreeing); a file, once taken, is gone.
    taken: set = set()
    for _fit, _order, t, f in sorted(vol_bids, key=lambda b: (b[0], b[1]), reverse=True):
        if t["owned"] or f in taken:
            continue
        t["owned"], t["file"] = True, f
        taken.add(f)
    # Second pass, loose: the catalogue says 'Batman Year 100 Deluxe Edition HC',
    # the file says 'Batman - Year 100 and Other Tales Deluxe Edition (2015)'. Same
    # book, different words around it. A no-volume trade still unowned claims an
    # unclaimed collected file whose significant words contain (or are contained
    # by) its own, with the edition words agreeing — after every exact match has
    # taken its file, so loose never steals from exact.
    claimed = {t["file"] for t in trades if t.get("file")}
    for t in trades:
        if t.get("owned") or t.get("vol") is not None or t.get("vol_range"):
            continue
        f = _loose_edition_file(t.get("title", ""), _edition_keywords(t.get("title", "")), owned_names, claimed,
                                series.get("title") or "")
        if f:
            t["owned"], t["file"] = True, f
            claimed.add(f)
    return trades


def _format_of(name: str) -> str | None:
    w = set(_norm_name(re.sub(r"\([^)]*\)", " ", name or "")).split())
    if w & {"hc", "hardcover"}:
        return "hc"
    if w & {"tp", "tpb", "paperback", "sc"}:
        return "tp"
    return None


def _trade_file_fit(t: dict, fname: str, landed: dict[str, str] | None) -> tuple:
    """How well a file on disk fits a catalogue edition, best first: downloaded AS
    this edition; share of significant title words (Jaccard, 'Vol 02' == 'Vol. 2');
    TP/HC agreeing (+1), disagreeing (-1), unknown (0)."""
    ids = {str(t.get("locg_id") or "")} | ({f"m{t['metron_issue_id']}"} if t.get("metron_issue_id") else set())
    was = 1 if landed and landed.get(fname.lower()) in ids - {""} else 0
    def words(s):
        return {w.lstrip("0") or "0" if w.isdigit() else w for w in _sig_words(s)}
    tw, fw = words(t.get("title") or ""), words(os.path.splitext(fname)[0])
    share = round(len(tw & fw) / len(tw | fw), 6) if tw | fw else 0.0
    a, b = _format_of(t.get("title") or ""), _format_of(os.path.splitext(fname)[0])
    if not a and t.get("format"):
        a = {"HC": "hc", "TPB": "tp", "TP": "tp"}.get(str(t["format"]).upper())
    fmt = 0 if not a or not b else (1 if a == b else -1)
    return (was, share, fmt)


_FORMAT_WORDS = frozenset({"tp", "tpb", "hc", "gn", "ogn", "sc", "edition", "collected", "collection", "hardcover",
                           "paperback", "the", "a", "an", "and", "of", "vol", "volume", "book"})


def _sig_words(name: str) -> set[str]:
    # scene tags — '(Digital) (Mephisto-Empire)', '(2020)' — are packaging, not title
    name = re.sub(r"\([^)]*\)", " ", name)
    return {w for w in _norm_name(name).split() if w not in _FORMAT_WORDS and not re.fullmatch(r"(19|20)\d\d", w)}


def _loose_edition_file(title: str, kws: frozenset, owned_names: dict[str, str], claimed: set, series_title: str) -> str | None:
    cw = _sig_words(title)
    sw = _sig_words(series_title)
    # one significant word is enough ONLY when it is the whole series name
    # ('100% TP' on a shelf called 100%) — otherwise two, so 'Batman' can't claim
    # every Batman-something in the folder
    if not cw or (len(cw) < 2 and cw != sw):
        return None
    for key, fname in owned_names.items():
        if fname in claimed or _parse_issue_number(fname, series_title) is not None:
            continue                                      # a single issue is never a trade
        if _edition_keywords(fname) != kws:
            continue                                      # a Deluxe file isn't the plain TP
        fw = _sig_words(os.path.splitext(fname)[0])
        if fw and (cw <= fw or fw <= cw) and (len(fw) >= 2 or fw == sw):
            return fname
    return None


def refresh_trades_owned(series_id: int) -> None:
    """Re-stamp owned/file on a series' cached trades without re-hitting LOCG —
    used right after a trade download so the tile flips to owned immediately
    instead of waiting for the next sync."""
    cached = db.get_trades(series_id, DB_PATH)
    if not cached:
        return
    series = db.get_series_by_id(series_id, DB_PATH)
    if not series:
        return
    enrich_trades(series, cached["trades"], landed=db.landed_trade_files(series_id, DB_PATH))
    db.set_trades(series_id, cached["trades"], DB_PATH)
    # A complete-run PACK lands as single issues, not a volume: the trade row
    # completes, the trades re-stamp sees nothing, and the issues sat at 0/6
    # until the next sync (Faithless, 2026-10-09). The folder is the truth for
    # issues too — reconcile them here, on every trade completion.
    try:
        rescan_owned(series)
    except Exception as e:
        logger.warning(f"Issue owned-rescan after trade failed for '{series.get('title')}': {e}")


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
    db.close_rows_now_owned(series["id"], DB_PATH)
    return {"scanned": True, "owned": len(owned_numbers)}
