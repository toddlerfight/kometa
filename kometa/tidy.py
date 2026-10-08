"""Tidy a series' files to the house convention — dry run first, then apply.

    Publisher/Series - Subtitle (Year)/Series - Subtitle #001 (Year).cbz

Agreed 2026-10-08 (docs/reader-spec.md, "Files"). Komga is being retired, so
the old reason not to touch files — Komga and the reader both key on paths —
is ours to solve, and it's solved here: every rename updates the book row, the
shelf row and the series folder in the same step, so read progress survives
(it hangs off the book id, not the path). CBRs become CBZs on the way.

What it does NOT do: guess. A file whose issue number can't be parsed (trades,
'Vol 1', oddities) is reported and left alone. Two files that would collide on
the same canonical name are both left alone and reported as duplicates. Moving
collected editions under their parent run and merging twin folders are
separate, later steps — they need a decision per case, not a rule.
"""
import logging
import os
import re

import kometa.db as db
from kometa.naming import parse_issue_number, is_variant_scan, format_issue_number, _safe, OWNED_EXTS

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH
CONVERT_EXTS = {".cbr", ".rar"}


class TidyError(RuntimeError):
    pass


def _run_identity(s: dict) -> dict:
    """What the run is actually called, and when it began — from Metron when the
    series is matched there, else what we already have."""
    title, year = s["title"], s.get("year_began")
    if s.get("metron_series_id"):
        try:
            from kometa import metron_client
            d = metron_client.series_detail(s["metron_series_id"])
            title = d.get("title") or title
            year = d.get("year") or year
        except Exception as e:
            logger.info(f"Tidy: Metron identity unavailable for {s['title']!r}, using ours: {e}")
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
    return {"title": title, "year": year}


def folder_name(title: str, year) -> str:
    """'Batman - Damned (2018)' — ':' becomes ' - ' (SMB-safe), year disambiguates."""
    t = _safe(re.sub(r"\s*:\s*", " - ", title))
    return f"{t} ({year})" if year else t


def file_name(title: str, number: float, year, ext: str) -> str:
    t = _safe(re.sub(r"\s*:\s*", " - ", title))
    y = f" ({year})" if year else ""
    return f"{t} #{format_issue_number(number)}{y}{ext}"


def _is_rar(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"Rar!"
    except OSError:
        return False


def plan(series_id: int, path=None) -> dict:
    """The dry run: every operation tidy WOULD do, and every file it would leave."""
    path = path or DB_PATH
    s = db.get_series_by_id(series_id, path)
    if not s:
        raise TidyError("No such series")
    folder = s.get("folder_path")
    if not folder or not os.path.isdir(folder):
        raise TidyError("The series has no folder on disk")
    ident = _run_identity(s)
    issue_years = {i["number"]: (i.get("store_date") or "")[:4] for i in db.get_issues_for_series(series_id, path)}

    target_folder = os.path.join(os.path.dirname(folder), folder_name(ident["title"], ident["year"]))
    ops, leave = [], []
    if os.path.realpath(target_folder) != os.path.realpath(folder):
        if os.path.exists(target_folder):
            raise TidyError(f"A folder named {os.path.basename(target_folder)!r} already exists — merge is a separate step")
        ops.append({"kind": "rename_folder", "from": folder, "to": target_folder})

    targets: dict[str, list[str]] = {}
    files = sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in OWNED_EXTS)
    parsed = []
    for f in files:
        if is_variant_scan(f):
            leave.append({"file": f, "reason": "cover/variant scan, not the issue"})
            continue
        num = parse_issue_number(f, s["title"])
        if num is None and ident["title"] != s["title"]:
            num = parse_issue_number(f, ident["title"])
        if num is None:
            leave.append({"file": f, "reason": "no issue number (trade, volume or odd name) — left as is"})
            continue
        src = os.path.join(folder, f)
        ext = os.path.splitext(f)[1].lower()
        convert = ext in CONVERT_EXTS or _is_rar(src)
        year = issue_years.get(num) or ident["year"]
        target = file_name(ident["title"], num, year, ".cbz" if convert else ext)
        targets.setdefault(target, []).append(f)
        parsed.append((f, num, target, convert))
    for f, num, target, convert in parsed:
        if len(targets[target]) > 1:
            leave.append({"file": f, "reason": f"duplicate of #{format_issue_number(num)} — pick one by hand"})
            continue
        if f == target and not convert:
            continue
        ops.append({"kind": "convert" if convert else "rename_file", "from": f, "to": target, "number": num})

    return {
        "series_id": series_id, "title": s["title"], "run_title": ident["title"], "year": ident["year"],
        "folder": folder, "target_folder": target_folder, "ops": ops, "leave": leave,
        "counts": {"rename_folder": sum(o["kind"] == "rename_folder" for o in ops),
                   "rename_file": sum(o["kind"] == "rename_file" for o in ops),
                   "convert": sum(o["kind"] == "convert" for o in ops), "leave": len(leave)},
    }


def apply(series_id: int, path=None) -> dict:
    """Do it. Recomputes the plan (never trusts a stale one), files first, folder
    last, database updated at every step. Returns what was done."""
    from kometa.downloader import ensure_cbz
    path = path or DB_PATH
    p = plan(series_id, path)
    folder = p["folder"]
    done = {"rename_file": 0, "convert": 0, "rename_folder": 0, "errors": []}
    for op in p["ops"]:
        if op["kind"] == "rename_folder":
            continue
        src = os.path.join(folder, op["from"])
        dst = os.path.join(folder, op["to"])
        try:
            if op["kind"] == "convert":
                mid = ensure_cbz(src)                      # verified rebuild; .cbr removed after
                if mid == src:
                    raise TidyError("CBR→CBZ repack didn't take")
                db.rename_book_path(src, mid, path)
                src = mid
            if src != dst:
                if os.path.exists(dst):
                    raise TidyError(f"{op['to']!r} already exists")
                os.rename(src, dst)
                db.rename_book_path(src, dst, path)
            done[op["kind"]] += 1
        except Exception as e:
            logger.warning(f"Tidy: {op['kind']} failed for {op['from']!r}: {e}")
            done["errors"].append({"file": op["from"], "error": str(e)})
    if any(o["kind"] == "rename_folder" for o in p["ops"]):
        try:
            os.rename(folder, p["target_folder"])
            db.move_folder_paths(series_id, folder, p["target_folder"], path)
            done["rename_folder"] = 1
            folder = p["target_folder"]
        except Exception as e:
            logger.warning(f"Tidy: folder rename failed for {folder!r}: {e}")
            done["errors"].append({"file": os.path.basename(folder), "error": str(e)})
    if p["run_title"] != p["title"]:
        db.set_series_title(series_id, p["run_title"], path)
    # ownership from disk, fresh — names changed under it
    try:
        from kometa.sync import rescan_owned
        rescan_owned(db.get_series_by_id(series_id, path))
    except Exception as e:
        logger.info(f"Tidy: rescan after apply failed: {e}")
    logger.info(f"Tidied {p['title']!r}: {done}")
    return {**done, "folder": folder, "title": p["run_title"]}
