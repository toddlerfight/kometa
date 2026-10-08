"""Single-file series: what each one actually is (report only — nothing moves).

414 of 876 series are one file. Three different things wear that count:
  one_shot   — a genuine one-shot / annual / OGN. 1/1 is correct.
  collected  — a trade, deluxe, omnibus filed as if it were a run. Belongs under
               its parent series as a trade.
  split      — one series spread one-issue-per-folder ('Batman & Grendel 01',
               '… 02'). Belongs merged into one folder.
  unknown    — can't tell yet (Metron type not fetched, name says nothing).

Metron's series type decides when we have it; the filename decides when we
don't. The report is the dry run for the per-case steps that follow.
"""
import logging
import os
import re
import threading

import kometa.db as db
from kometa.naming import parse_issue_number, norm_key, OWNED_EXTS

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH

ONE_SHOT_TYPES = {"one-shot", "one shot", "annual", "graphic novel", "original graphic novel", "ogn"}
COLLECTED_TYPES = {"trade paperback", "tpb", "hard cover", "hardcover", "omnibus", "collected edition",
                   "digest", "absolute edition"}
RUN_TYPES = {"ongoing series", "limited series", "cancelled series", "single issue"}

_COLLECTED_WORDS = re.compile(
    r"\b(deluxe|omnibus|tpb|hc|hardcover|absolute|compendium|collection|collected|anniversary|"
    r"library|treasury|edition|vol(?:ume)?\.?\s*\d|book\s+(?:one|two|three|\d))\b", re.I)
_SPLIT_SUFFIX = re.compile(r"^(?P<base>.+?)\s+(?:#\s*)?0?(?P<n>\d{1,2})(?:\s*[-–:]\s*(?P<sub>.+))?(?:\s*\[.*\])?$")


def _single_file(folder: str) -> str | None:
    try:
        files = [f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in OWNED_EXTS]
    except OSError:
        return None
    return files[0] if len(files) == 1 else None


def _by_type(metron_type: str | None) -> str | None:
    t = (metron_type or "").strip().lower()
    if not t:
        return None
    if t in ONE_SHOT_TYPES:
        return "one_shot"
    if t in COLLECTED_TYPES:
        return "collected"
    if t in RUN_TYPES:
        return "run"
    return None


def classify(series: dict, filename: str, siblings: dict[str, list[str]] | None = None) -> dict:
    """{kind, why, parent_guess?, split_base?}. Metron type first, then the name."""
    title = series["title"]
    kind = _by_type(series.get("metron_type"))
    if kind == "collected":
        return {"kind": "collected", "why": f"Metron: {series['metron_type']}", "parent_guess": _parent_guess(title)}
    if kind == "one_shot":
        return {"kind": "one_shot", "why": f"Metron: {series['metron_type']}"}
    m = _SPLIT_SUFFIX.match(title)
    if m and siblings is not None:
        base = norm_key(m.group("base"))
        if len(siblings.get(base, [])) > 1:
            return {"kind": "split", "why": f"one of {len(siblings[base])} folders named '{m.group('base')} NN'",
                    "split_base": m.group("base")}
    if _COLLECTED_WORDS.search(title) or _COLLECTED_WORDS.search(filename):
        return {"kind": "collected", "why": "name says collected edition", "parent_guess": _parent_guess(title)}
    if parse_issue_number(filename, title) is None:
        return {"kind": "collected", "why": "file has no issue number", "parent_guess": _parent_guess(title)}
    if kind == "run":
        n = series.get("metron_issue_count")
        return {"kind": "one_shot" if n == 1 else "unknown",
                "why": f"Metron: {series['metron_type']}" + (f", {n} issues" if n else "")}
    return {"kind": "unknown", "why": "Metron type not known yet" if series.get("metron_series_id") and series.get("metron_type") is None
            else "no catalogue match, name says nothing"}


def _parent_guess(title: str) -> str | None:
    """'Batman - Year One - The Deluxe Edition' → 'Batman'. A guess for the report;
    the real parent comes from the trade's reprints later."""
    t = re.sub(r"\(\d{4}\)", "", title)
    t = _COLLECTED_WORDS.sub("", t)
    head = re.split(r"\s+[-–:]\s+|:\s*", t, maxsplit=1)[0].strip(" -–:")
    return head or None


def report(path=None) -> dict:
    path = path or DB_PATH
    series = [s for s in db.get_all_series(path) if s.get("kind") != "arc" and s.get("folder_path")]
    singles = []
    for s in series:
        f = _single_file(s["folder_path"])
        if f:
            singles.append((s, f))
    siblings: dict[str, list[str]] = {}
    for s, _ in singles:
        m = _SPLIT_SUFFIX.match(s["title"])
        if m:
            siblings.setdefault(norm_key(m.group("base")), []).append(s["title"])
    shelf_titles = {norm_key(re.sub(r"\(\d{4}\)", "", s["title"])) for s in series}
    rows = []
    for s, f in singles:
        c = classify(s, f, siblings)
        pg = c.get("parent_guess")
        rows.append({"id": s["id"], "title": s["title"], "publisher": s.get("publisher"), "file": f,
                     "match_status": s.get("match_status"), "metron_type": s.get("metron_type"),
                     **c, "parent_on_shelf": bool(pg and norm_key(pg) in shelf_titles and norm_key(pg) != norm_key(s["title"]))})
    counts = {k: sum(r["kind"] == k for r in rows) for k in ("one_shot", "collected", "split", "unknown")}
    typed = sum(1 for s, _ in singles if s.get("metron_type") is not None)
    matched = sum(1 for s, _ in singles if s.get("metron_series_id"))
    return {"total_series": len(series), "singles": len(rows), "counts": counts,
            "types_known": typed, "types_pending": matched - typed, "rows": rows}


# --- Metron type backfill ------------------------------------------------------------
_job = threading.Lock()


def backfill_types(limit: int | None = None) -> int:
    """Fetch the Metron type for matched series that don't have one yet. Metron
    pace (3.5s each) — ~400 series is twenty-odd minutes, in the background."""
    from kometa import metron_client
    if not metron_client.configured() or not _job.acquire(blocking=False):
        return 0
    try:
        todo = [s for s in db.get_all_series(DB_PATH)
                if s.get("metron_series_id") and s.get("metron_type") is None and s.get("kind") != "arc"]
        n = 0
        for s in todo[:limit] if limit else todo:
            try:
                db.set_metron_type(s["id"], metron_client.series_detail(s["metron_series_id"]).get("type"), DB_PATH)
                n += 1
            except metron_client.MetronUnavailable as e:
                logger.info(f"Type backfill paused: {e}")
                break
        if n:
            logger.info(f"Metron types filled for {n} series")
        return n
    finally:
        _job.release()


def backfill_in_background():
    threading.Thread(target=backfill_types, name="metron-types", daemon=True).start()
