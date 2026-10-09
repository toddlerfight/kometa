"""Twin folders: one run, two folders. Merge the second into the first.

The import logs these ('duplicate folder … left on the shelf'): a shelf folder
nobody added whose title is the same series as one you already have —
'Event Horizon- Dark Descent' beside 'Event Horizon - Dark Descent', Last
Ronin under both IDW and Mirage. The series' own folder is kept. The twin's
files move in; an issue present in BOTH keeps the better copy (CBZ over CBR,
then the bigger file) and the other goes to _trash with an origin marker, same
as Remove; files with no issue number move as they are. Book rows follow every
move, so read progress survives. Plan first; nothing moves until Apply.
"""
import logging
import os
import re
import shutil
import time

import kometa.db as db
from kometa import sources
from kometa.naming import norm_key, parse_issue_number, OWNED_EXTS
from kometa.reader import READER_ID

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH


class TwinError(RuntimeError):
    pass


def _key(title: str) -> str:
    return norm_key(re.sub(r"\(\d{4}\)", "", title).strip().strip("-").strip())


# 'Volume 01', 'Book One', '03': a name that says nothing about WHICH run.
# Two such folders are not twins however equal the names (Dogs of London and
# The Boogyman both have a 'Volume 01 (2022)').
_GENERIC = re.compile(r"^(vol(?:ume)?\s*\d+|book\s+(?:\w+)|\d+|part\s*\d+|tpb|hc)$")


def _same_publisher(a: str | None, b: str | None) -> bool:
    from kometa.naming import _pub_key
    return not a or not b or _pub_key(a) == _pub_key(b)


def find_twins(path=None) -> list[dict]:
    """[{shelf_id, folder, title, publisher, book_count, series_id, series_title, series_folder}]"""
    path = path or DB_PATH
    series = [s for s in db.get_all_series(path) if s.get("kind") != "arc" and s.get("folder_path")]
    by_key: dict[str, dict] = {}
    for s in series:
        by_key.setdefault(_key(s["title"]), s)
    out = []
    for sh in db.list_shelf(READER_ID, untracked_only=True, path=path):
        k = _key(sh["title"])
        if _GENERIC.match(k):
            continue
        s = by_key.get(k)
        if not s or os.path.realpath(s["folder_path"]) == os.path.realpath(sh["folder_path"]):
            continue
        out.append({"shelf_id": sh["id"], "folder": sh["folder_path"], "title": sh["title"], "publisher": sh.get("publisher"),
                    "book_count": sh.get("book_count"), "series_id": s["id"], "series_title": s["title"],
                    "series_folder": s["folder_path"], "series_publisher": s.get("publisher"),
                    # same name, different publisher: usually a misfiled folder (Last Ronin under
                    # Mirage), sometimes a different book entirely (Atlas's Fear Itself). Flagged, not hidden.
                    "publisher_differs": not _same_publisher(sh.get("publisher"), s.get("publisher"))})
    # Two TRACKED series on one catalogue id are one run twice — 'I Feel Sick'
    # and 'I Feel Sick - A Book about a Girl', both LOCG 111869. Neither folder is
    # untracked, so the title walk above never sees them. The one with fewer
    # files is the twin; the other keeps the run.
    by_cat: dict[tuple, list] = {}
    for s in series:
        for col in ("metron_series_id", "locg_series_id"):
            if s.get(col):
                by_cat.setdefault((col, s[col]), []).append(s)
    seen = set()
    for (col, cid), group in by_cat.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (-len(_files(s["folder_path"])), s["id"]))
        keeper = group[0]
        for twin in group[1:]:
            if twin["id"] in seen or os.path.realpath(twin["folder_path"]) == os.path.realpath(keeper["folder_path"]):
                continue
            seen.add(twin["id"])
            sh = next((x for x in db.list_shelf(READER_ID, untracked_only=False, path=path)
                       if os.path.realpath(x["folder_path"]) == os.path.realpath(twin["folder_path"])), None)
            if not sh:
                continue
            out.append({"shelf_id": sh["id"], "folder": twin["folder_path"], "title": twin["title"], "publisher": twin.get("publisher"),
                        "book_count": sh.get("book_count"), "series_id": keeper["id"], "series_title": keeper["title"],
                        "series_folder": keeper["folder_path"], "series_publisher": keeper.get("publisher"),
                        "publisher_differs": not _same_publisher(twin.get("publisher"), keeper.get("publisher")),
                        "same_run": True, "twin_series_id": twin["id"]})
    out.sort(key=lambda r: (r["publisher_differs"], r["title"].lower()))
    return out


def _same_run(sh: dict, s: dict, path) -> dict | None:
    """The twin folder's own tracked series, if it shares a catalogue id with `s`."""
    t = sh.get("tracked_series_id") and db.get_series_by_id(sh["tracked_series_id"], path)
    if not t or t["id"] == s["id"]:
        return None
    for col in ("metron_series_id", "locg_series_id"):
        if s.get(col) and t.get(col) == s[col]:
            return t
    return None


def _files(folder: str) -> list[str]:
    try:
        return sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in OWNED_EXTS and not f.startswith("."))
    except OSError:
        return []


def _better(a_path: str, b_path: str) -> str:
    """The copy to keep: CBZ beats CBR, then the bigger file."""
    def rank(p):
        return (p.lower().endswith(".cbz"), os.path.getsize(p) if os.path.exists(p) else 0)
    return a_path if rank(a_path) >= rank(b_path) else b_path


def plan(shelf_id: int, series_id: int, path=None) -> dict:
    path = path or DB_PATH
    sh = db.get_shelf_series(shelf_id, path)
    s = db.get_series_by_id(series_id, path)
    if not sh or not s:
        raise TwinError("No such folder or series")
    twin_series = _same_run(sh, s, path)
    if sh.get("tracked_series_id") and not twin_series:
        raise TwinError("That folder is already a series of its own")
    src, dst = sh["folder_path"], s.get("folder_path")
    if not os.path.isdir(src) or not dst or not os.path.isdir(dst):
        raise TwinError("A folder is missing on disk")
    have = {}
    for f in _files(dst):
        n = parse_issue_number(f, s["title"])
        if n is not None:
            have.setdefault(n, f)
    moves, dupes, asis = [], [], []
    for f in _files(src):
        n = parse_issue_number(f, sh["title"]) or parse_issue_number(f, s["title"])
        if n is None:
            asis.append({"file": f})
            continue
        if n in have:
            keep = _better(os.path.join(src, f), os.path.join(dst, have[n]))
            dupes.append({"number": n, "twin_file": f, "series_file": have[n], "keep": "twin" if keep.startswith(src + "/") else "series"})
        else:
            moves.append({"number": n, "file": f})
    return {"shelf_id": shelf_id, "series_id": series_id, "twin_title": sh["title"], "series_title": s["title"],
            "twin_series_id": twin_series["id"] if twin_series else None,
            "twin_folder": src, "series_folder": dst, "moves": moves, "duplicates": dupes, "as_is": asis,
            "counts": {"move": len(moves) + len(asis), "duplicate": len(dupes),
                       "twin_wins": sum(d["keep"] == "twin" for d in dupes)}}


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
    logger.info(f"Twin merge: binned {os.path.basename(path_)} ({why})")


def _move(a: str, b: str, series_id: int, shelf_id: int, number, dbp):
    if os.path.exists(b):
        stem, ext = os.path.splitext(b)
        b = f"{stem} ({int(time.time())}){ext}"
    os.rename(a, b)
    db.rename_book_path(a, b, dbp)
    db.set_book_owner(b, series_id, shelf_id, number=number, path=dbp)
    return b


def apply(shelf_id: int, series_id: int, path=None, root: str | None = None) -> dict:
    path = path or DB_PATH
    root = root or sources.comics_root()
    p = plan(shelf_id, series_id, path)
    src, dst = p["twin_folder"], p["series_folder"]
    keep_shelf = db.shelf_id_for_series(series_id, path)
    done = {"moved": 0, "binned": 0, "errors": []}
    for d in p["duplicates"]:
        try:
            if d["keep"] == "twin":
                _bin(os.path.join(dst, d["series_file"]), root, f"duplicate of #{d['number']:g}, twin's copy is better", path)
                _move(os.path.join(src, d["twin_file"]), os.path.join(dst, d["twin_file"]), series_id, keep_shelf, d["number"], path)
                done["moved"] += 1
            else:
                _bin(os.path.join(src, d["twin_file"]), root, f"duplicate of #{d['number']:g}", path)
            done["binned"] += 1
        except Exception as e:
            done["errors"].append({"file": d["twin_file"], "error": str(e)})
    for m in p["moves"]:
        try:
            _move(os.path.join(src, m["file"]), os.path.join(dst, m["file"]), series_id, keep_shelf, m["number"], path)
            done["moved"] += 1
        except Exception as e:
            done["errors"].append({"file": m["file"], "error": str(e)})
    for a in p["as_is"]:
        try:
            _move(os.path.join(src, a["file"]), os.path.join(dst, a["file"]), series_id, keep_shelf, None, path)
            done["moved"] += 1
        except Exception as e:
            done["errors"].append({"file": a["file"], "error": str(e)})
    if not done["errors"]:
        db.remove_books_under(src, path)
        db.remove_shelf_series_by_path(src, path)
        if p.get("twin_series_id"):
            # the twin was a tracked series of the same run: its row goes with its folder
            db.dequeue_waiting_series(p["twin_series_id"], path)
            db.remove_series(p["twin_series_id"], path)
        leftovers = [f for f in os.listdir(src) if not f.startswith(".")]
        if not leftovers:
            shutil.rmtree(src, ignore_errors=True)
            # The kept folder may be the badly named one ('- Justice League', the
            # arc feature's doing) while the twin had the clean name. Now that the
            # twin's name is free, take it.
            if os.path.basename(dst).startswith("-") and os.path.dirname(dst) == os.path.dirname(src) \
                    and not os.path.exists(src):
                os.rename(dst, src)
                db.move_folder_paths(series_id, dst, src, path)
                logger.info(f"Twin merge: renamed {os.path.basename(dst)!r} -> {os.path.basename(src)!r}")
                done["renamed_to"] = src
                dst = src
            # an emptied publisher folder (Mirage/, Splitter/) goes too
            parent = os.path.dirname(src)
            try:
                if parent != root and not [f for f in os.listdir(parent) if not f.startswith(".")]:
                    shutil.rmtree(parent, ignore_errors=True)
            except OSError:
                pass
    try:
        from kometa.sync import rescan_owned
        rescan_owned(db.get_series_by_id(series_id, path))
    except Exception as e:
        logger.info(f"Twin merge: rescan failed: {e}")
    logger.info(f"Merged twin {p['twin_title']!r} into {p['series_title']!r}: {done}")
    return {**done, "series_id": series_id, "series_title": p["series_title"]}
