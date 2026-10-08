"""Combine: many one-file folders that are one series → one series, numbered.

'The Adventures of Tintin - The Blue Lotus', '… - The Black Island', ×25; the
eight 'Batman - One Bad Day - <villain>' one-shots. No catalogue has them as a
run, so the shelf became 25 stub series of one issue each. Combine groups them
by the shared prefix before ' - ', proposes an order (year in the file name,
else the catalogue's, else alphabetical), and on Apply moves every file into
one folder as 'Series #NN - Subtitle (Year).cbz' — the subtitle kept, so the
grid reads 1..N and still says which album is which. Stub series go, book rows
and progress follow, the result is a 'no run' series. Dry run first.

Files first, folders last — never rename a folder and then its files on this
share (OrbStack's file sharing holds the old directory handle and the file
rename fails with EINVAL).
"""
import logging
import os
import re
import shutil
import time
from collections import defaultdict

import kometa.db as db
from kometa import sources
from kometa.naming import norm_key, parse_issue_number, _safe, OWNED_EXTS
from kometa.reader import READER_ID

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH
MIN_GROUP = 3
_YEAR_RE = re.compile(r"\((\d{4})\)")
_SPLIT_RE = re.compile(r"\s+-\s+|:\s+|\s+–\s+")


class CombineError(RuntimeError):
    pass


def _files(folder: str) -> list[str]:
    try:
        return sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in OWNED_EXTS and not f.startswith("."))
    except OSError:
        return []


def _split(title: str) -> tuple[str, str] | None:
    """'The Adventures of Tintin - The Blue Lotus' → ('The Adventures of Tintin', 'The Blue Lotus')."""
    t = _YEAR_RE.sub("", title).strip()
    parts = _SPLIT_RE.split(t, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


def _year_of(folder: str, files: list[str], series: dict) -> int | None:
    ys = sorted({int(y) for f in files for y in _YEAR_RE.findall(f) if 1930 <= int(y) <= 2100})
    if ys:
        return ys[0]
    m = _YEAR_RE.search(series.get("title") or "")
    return int(m.group(1)) if m else series.get("year_began")


def _member(s: dict, names: dict[int, list[str]] | None = None) -> dict | None:
    folder = s.get("folder_path")
    if not folder:
        return None
    # the books table knows the files (one query for the shelf); the disk is only
    # asked when a caller has no map — a listing per series over SMB took 47 s
    files = ([f for f in names[s["id"]] if os.path.splitext(f)[1].lower() in OWNED_EXTS]
             if names is not None and s["id"] in names else _files(folder))
    if not files:
        return None
    sp = _split(s["title"])
    if not sp:
        return None
    return {"id": s["id"], "title": s["title"], "prefix": sp[0], "subtitle": sp[1], "folder": folder,
            "files": files, "year": _year_of(folder, files, s), "publisher": s.get("publisher"),
            "match_status": s.get("match_status"),
            # already its own catalogue run (the Metal one-shots, the Hellboy specials):
            # combining would throw that link away — shown, but unticked by default
            "matched": bool(s.get("metron_series_id") or s.get("locg_series_id"))}


def find_groups(path=None) -> list[dict]:
    """Groups of ≥MIN_GROUP one-file series sharing a prefix (same publisher folder)."""
    path = path or DB_PATH
    groups: dict[tuple, list] = defaultdict(list)
    names = db.book_names_by_series(path)
    for s in db.get_all_series(path):
        if s.get("kind") == "arc":
            continue
        m = _member(s, names)
        if not m or len(m["files"]) > 3:
            continue                                   # a real run with many issues isn't an album folder
        groups[(norm_key(m["prefix"]), os.path.dirname(m["folder"]))].append(m)
    out = []
    for (key, parent), members in groups.items():
        if len(members) < MIN_GROUP:
            continue
        members.sort(key=lambda m: ((m["year"] or 9999), m["subtitle"].lower()))
        out.append({"prefix": members[0]["prefix"], "parent": parent, "publisher": members[0]["publisher"],
                    "count": len(members), "ids": [m["id"] for m in members],
                    "members": [{"id": m["id"], "subtitle": m["subtitle"], "year": m["year"], "files": len(m["files"]),
                                 "title": m["title"], "matched": m["matched"]} for m in members],
                    "unmatched": sum(1 for m in members if not m["matched"])})
    out.sort(key=lambda g: -g["count"])
    return out


def _best(folder: str, files: list[str]) -> str:
    def rank(f):
        p = os.path.join(folder, f)
        return (f.lower().endswith(".cbz"), "(" in f, os.path.getsize(p) if os.path.exists(p) else 0)
    return max(files, key=rank)


def plan(ids: list[int], title: str | None = None, order: str | list[int] = "year", path=None) -> dict:
    """What Combine would do. order: 'year' | 'alpha' | an explicit list of ids."""
    path = path or DB_PATH
    members = []
    for sid in ids:
        s = db.get_series_by_id(sid, path)
        m = s and _member(s)
        if not m:
            raise CombineError(f"Series {sid} has no folder with files, or no 'Series - Subtitle' name")
        members.append(m)
    if len(members) < 2:
        raise CombineError("Pick at least two")
    title = (title or members[0]["prefix"]).strip()
    if isinstance(order, list):
        pos = {sid: i for i, sid in enumerate(order)}
        members.sort(key=lambda m: pos.get(m["id"], 999))
    elif order == "alpha":
        members.sort(key=lambda m: m["subtitle"].lower())
    else:
        members.sort(key=lambda m: ((m["year"] or 9999), m["subtitle"].lower()))
    parent = os.path.dirname(members[0]["folder"])
    target = os.path.join(parent, _safe(re.sub(r"\s*:\s*", " - ", title)))
    items, dupes = [], []
    for n, m in enumerate(members, 1):
        keep = _best(m["folder"], m["files"])
        ext = os.path.splitext(keep)[1].lower()
        y = f" ({m['year']})" if m["year"] else ""
        new = f"{_safe(re.sub(r'\s*:\s*', ' - ', title))} #{n:02d} - {_safe(m['subtitle'])}{y}{ext}"
        items.append({"n": n, "id": m["id"], "subtitle": m["subtitle"], "year": m["year"], "from": keep, "to": new,
                      "folder": m["folder"]})
        for f in m["files"]:
            if f != keep:
                dupes.append({"id": m["id"], "file": f, "kept": keep})
    existing = [s for s in db.get_all_series(path) if s.get("folder_path") and os.path.realpath(s["folder_path"]) == os.path.realpath(target)
                and s["id"] not in ids]
    if existing or (os.path.exists(target) and os.path.realpath(target) not in {os.path.realpath(m["folder"]) for m in members}):
        raise CombineError(f"A folder named {os.path.basename(target)!r} already exists")
    return {"title": title, "target": target, "publisher": members[0]["publisher"], "items": items, "duplicates": dupes,
            "counts": {"albums": len(items), "duplicates": len(dupes)}}


def _mv(a: str, b: str):
    try:
        os.rename(a, b)
    except OSError:
        shutil.copy2(a, b)
        os.remove(a)


def _bin(path_: str, root: str, why: str, dbp):
    rel = os.path.relpath(path_, root)
    dest = os.path.join(root, "_trash", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        dest = f"{dest} ({int(time.time())})"
    shutil.move(path_, dest)
    marker = os.path.join(os.path.dirname(dest), ".kometa-origin")
    if not os.path.exists(marker):
        with open(marker, "w") as f:
            f.write(os.path.dirname(path_))
    with db._connect(dbp) as c:
        c.execute("DELETE FROM read_progress WHERE book_id IN (SELECT id FROM books WHERE path = ?)", (path_,))
        c.execute("DELETE FROM books WHERE path = ?", (path_,))
    logger.info(f"Combine: binned {os.path.basename(path_)} ({why})")


def apply(ids: list[int], title: str | None = None, order: str | list[int] = "year", path=None, root: str | None = None) -> dict:
    path = path or DB_PATH
    root = root or sources.comics_root()
    p = plan(ids, title, order, path)
    target = p["target"]
    os.makedirs(target, exist_ok=True)
    first = db.get_series_by_id(p["items"][0]["id"], path)
    new_id = db.add_series(title=p["title"], publisher=p["publisher"], folder_path=target,
                           year_began=p["items"][0]["year"], on_pull_list=False, path=path)
    db.set_match_status(new_id, "manual", path)
    db.set_metron_link(new_id, "none", path)
    db.set_locg_link(new_id, "none", path)
    shelf_id = db.upsert_shelf_series(target, p["title"], p["publisher"], new_id, len(p["items"]),
                                      time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()), path)
    done = {"moved": 0, "binned": 0, "errors": [], "series_id": new_id, "title": p["title"]}
    for d in p["duplicates"]:
        try:
            m = next(i for i in p["items"] if i["id"] == d["id"])
            _bin(os.path.join(m["folder"], d["file"]), root, f"duplicate, kept {d['kept']!r}", path)
            done["binned"] += 1
        except Exception as e:
            done["errors"].append({"file": d["file"], "error": str(e)})
    for it in p["items"]:
        src, dst = os.path.join(it["folder"], it["from"]), os.path.join(target, it["to"])
        try:
            if os.path.realpath(src) != os.path.realpath(dst):
                _mv(src, dst)
            db.rename_book_path(src, dst, path)
            db.set_book_owner(dst, new_id, shelf_id, number=float(it["n"]), path=path)
            done["moved"] += 1
        except Exception as e:
            done["errors"].append({"file": it["from"], "error": str(e)})
    if not done["errors"]:
        for it in p["items"]:
            if os.path.realpath(it["folder"]) == os.path.realpath(target):
                continue
            db.dequeue_waiting_series(it["id"], path)
            db.remove_books_under(it["folder"], path)
            db.remove_shelf_series_by_path(it["folder"], path)
            db.remove_series(it["id"], path)
            try:
                if not [f for f in os.listdir(it["folder"]) if not f.startswith(".")]:
                    shutil.rmtree(it["folder"], ignore_errors=True)
            except OSError:
                pass
    try:
        from kometa.sync import rescan_owned
        rescan_owned(db.get_series_by_id(new_id, path))
    except Exception as e:
        logger.info(f"Combine: rescan failed: {e}")
    logger.info(f"Combined {len(p['items'])} folders into {p['title']!r}: {done}")
    return done
