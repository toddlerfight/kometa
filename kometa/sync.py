"""Per-series sync — reconcile one tracked series against Komga (ownership +
book IDs) and the metadata sources (Metron primary, ComicVine and LOCG as
supplements), then upsert the merged issue list.

Ownership is what's on disk; the Komga book map only supplies book IDs for
thumbnails. Metron is authoritative for the issue list; CV/LOCG fill gaps and
add upcoming solicitations.
"""
import os
import logging

from kometa.sources import (
    komga as _komga, metron as _metron, comicvine as _comicvine, locg as _locg,
)
from kometa.naming import (
    scan_folder_numbers as _scan_folder_numbers, parse_issue_number as _parse_issue_number,
)
from kometa.locg_client import get_issues_anon
import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH


def sync_one(series: dict):
    komga = _komga()
    metron = _metron()

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

    # Komga book map — used for ownership supplement and book IDs (thumbnails)
    book_map: dict[float, str] = {}
    if series.get("komga_series_id") and komga:
        try:
            for b in komga.get_books(series["komga_series_id"]):
                if b.get("media", {}).get("status") == "ERROR":
                    continue
                n = b["metadata"]["numberSort"]
                if n is None:
                    continue
                key = float(n)
                if key in book_map:
                    # numberSort collision = Komga metadata is wrong on at least one book.
                    # Prefer whichever book's filename actually matches the key.
                    fn_num = _parse_issue_number(b.get("name", ""), series.get("title", ""))
                    logger.warning(
                        "numberSort collision at %s for '%s': %s vs %s",
                        key, series.get("title"), book_map[key], b["id"]
                    )
                    if fn_num == key:
                        book_map[key] = b["id"]
                else:
                    book_map[key] = b["id"]
        except Exception:
            pass

    # Ownership = what's on disk. book_map is only used to populate komga_book_id for thumbnails.
    folder = series.get("folder_path")
    if folder and os.path.isdir(folder):
        owned_numbers = _scan_folder_numbers(folder, series.get("title", ""))
    else:
        owned_numbers = set(book_map.keys())

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
    db.mark_synced(series["id"], DB_PATH)
