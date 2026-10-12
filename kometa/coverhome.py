"""Covers live with the comics (docs/library-enrichment-spec.md, step 2).

The cover store (data/covers on ginbako) holds art the catalogues may stop
serving — variants, ComicVine originals, PRH print covers. Kept only there it's
one disk away from gone and eats ginbako's space. Its permanent home is beside
the comics on the NAS, in each series folder:

    <series>/.kometa/covers/
        index.json               key → file, url, source, bytes (rebuilds the store)
        series.jpg
        <issue>/main.jpg  cv.jpg  prh.jpg  variant-<id>.jpg ...
        trades/<key>.jpg

Dot-folder: the shelf scan skips it (shelf._skip), and .jpg is never a comic.
The nightly comics mirror to Voot II carries it for free. Covers made from page
1 of the file itself ('file' keys) stay out — the comic regenerates them.

plan() reads only. copy() writes — and only when someone has said go.
"""
import json
import logging
import os
import re
import shutil
import tempfile

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
HOME = os.path.join(".kometa", "covers")
INDEX = "index.json"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _num_dir(n: str) -> str:
    """Issue folder name: '1' → '001', '605.1' → '605.1', '1/2' keys never occur ('0.5')."""
    try:
        f = float(n)
    except ValueError:
        return _SAFE.sub("_", n)
    return f"{int(f):03d}" if f == int(f) else f"{f:g}"


def placement(key: str) -> tuple[int, str] | None:
    """Store key → (tracked series id, path under the series' covers home), or
    None for keys that don't belong in a series folder (file-made covers, the
    external catalogue-card cache, book/<id> shelf covers)."""
    parts = key.split("/")
    if parts[0] == "issue" and len(parts) >= 4 and parts[1].isdigit():
        sid, num, kind = int(parts[1]), parts[2], parts[3]
        if kind == "file":
            return None
        if kind == "variant" and len(parts) >= 5:
            name = "variant-" + _SAFE.sub("_", "/".join(parts[4:])) + ".jpg"
        else:
            name = _SAFE.sub("_", kind) + ".jpg"
        return sid, os.path.join(_num_dir(num), name)
    if parts[0] == "series" and len(parts) == 2 and parts[1].isdigit():
        return int(parts[1]), "series.jpg"
    if parts[0] == "trade" and len(parts) == 2:
        sid = parts[1].split("-", 1)[0]
        if sid.isdigit():
            return int(sid), os.path.join("trades", _SAFE.sub("_", parts[1]) + ".jpg")
    return None


def plan(path=None) -> dict:
    """What a copy would write: per series folder, every stored cover that has a
    home, what's already there and current (same size), and the totals. Reads
    the DB and stats files; writes nothing."""
    path = path or DB_PATH
    folders = {s["id"]: s.get("folder_path") for s in db.get_all_series(path) if s.get("kind") != "arc"}
    with db._connect(path) as conn:
        rows = conn.execute("SELECT key, path, url, bytes, source FROM image_record WHERE path IS NOT NULL").fetchall()
    out = {"series": {}, "files": 0, "bytes": 0, "already": 0, "no_folder": 0, "missing_source": 0, "skipped_keys": 0}
    for r in rows:
        where = placement(r["key"])
        if not where:
            out["skipped_keys"] += 1
            continue
        sid, rel = where
        folder = folders.get(sid)
        if not folder or not os.path.isdir(folder):
            out["no_folder"] += 1
            continue
        if not os.path.exists(r["path"]):
            out["missing_source"] += 1
            continue
        size = os.path.getsize(r["path"])
        dest = os.path.join(folder, HOME, rel)
        s = out["series"].setdefault(sid, {"folder": folder, "items": [], "bytes": 0})
        item = {"key": r["key"], "src": r["path"], "dest": dest, "rel": rel, "bytes": size,
                "url": r["url"], "source": r["source"]}
        if os.path.exists(dest) and os.path.getsize(dest) == size:
            out["already"] += 1
            item["current"] = True
        else:
            out["files"] += 1
            out["bytes"] += size
            s["bytes"] += size
        s["items"].append(item)
    out["folders"] = len(out["series"])
    return out


def summary(p: dict) -> dict:
    """The dry run, the way a person reads it."""
    return {"series_folders": p["folders"], "files_to_write": p["files"], "gb_to_write": round(p["bytes"] / 1e9, 2),
            "already_there": p["already"], "covers_without_a_series_folder": p["no_folder"],
            "store_rows_missing_their_file": p["missing_source"], "not_for_the_nas": p["skipped_keys"]}


def _write_atomic(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".kometa-tmp", dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError(f"short copy: {dest}")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_index(folder: str, items: list[dict]) -> None:
    home = os.path.join(folder, HOME)
    idx = os.path.join(home, INDEX)
    try:
        with open(idx) as fh:
            current = json.load(fh)
    except (OSError, ValueError):
        current = {}
    for it in items:
        current[it["key"]] = {"file": it["rel"], "url": it["url"], "source": it["source"], "bytes": it["bytes"]}
    os.makedirs(home, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".kometa-tmp", dir=home)
    with os.fdopen(fd, "w") as fh:
        json.dump(current, fh, indent=1, sort_keys=True)
    os.replace(tmp, idx)


def copy(p: dict | None = None, path=None, limit_series: int | None = None, sleep=None) -> dict:
    """Write what plan() found: each cover copied (temp file, size check, atomic
    rename), then the series' index.json. Idempotent — a current file is left
    alone. Only for when the user has said go (step 2b)."""
    p = p or plan(path)
    done = {"series": 0, "files": 0, "bytes": 0, "errors": []}
    from kometa.activity import yield_to_reader
    for i, (sid, s) in enumerate(sorted(p["series"].items())):
        if limit_series is not None and i >= limit_series:
            break
        if sleep:
            yield_to_reader(sleep)
        wrote = []
        for it in s["items"]:
            if it.get("current"):
                wrote.append(it)
                continue
            try:
                _write_atomic(it["src"], it["dest"])
            except OSError as e:
                done["errors"].append(f"{it['dest']}: {e}")
                continue
            done["files"] += 1
            done["bytes"] += it["bytes"]
            wrote.append(it)
        if wrote:
            _write_index(s["folder"], wrote)
        done["series"] += 1
    logger.info(f"Covers home: {done['files']} files, {done['bytes'] / 1e9:.2f} GB into {done['series']} series folders"
                + (f", {len(done['errors'])} errors" if done["errors"] else ""))
    return done


# --- the standing copy (2e) and the way back (2c) --------------------------------------
def sync_tick(path=None) -> dict:
    """Scheduler (every 30 min): new covers in the store go home too — only what
    isn't there yet, so a quiet half-hour costs one plan (seconds). Off with
    config coverhome_enabled = '0'."""
    path = path or DB_PATH
    if db.get_config(path).get("coverhome_enabled", "1") == "0":
        return {"enabled": False}
    p = plan(path)
    if not p["files"]:
        return {"enabled": True, "files": 0}
    import time
    return copy(p, path, sleep=time.sleep)


def restore(path=None, root: str | None = None, dry_run: bool = False) -> dict:
    """2c: the NAS copy is the permanent one. Every series folder's index.json says
    which store key each file is; a key whose local file is missing (or a store that
    was wiped) gets the NAS file copied back and its image_record row pointed at it.
    A local file that's present is left alone."""
    from kometa import images, sources
    path = path or DB_PATH
    root = root or sources.comics_root()
    images.ensure_tables(path)
    out = {"indexes": 0, "keys": 0, "restored": 0, "already": 0, "missing_on_nas": 0, "errors": []}
    for dirpath, dirnames, filenames in os.walk(root):
        # only descend into series folders and their .kometa/covers; skip trash/quarantine
        dirnames[:] = [d for d in dirnames if not d.startswith(("_", "@", "#")) and (d == ".kometa" or d == "covers" or not d.startswith("."))]
        if INDEX not in filenames or not dirpath.endswith(HOME):
            continue
        out["indexes"] += 1
        try:
            with open(os.path.join(dirpath, INDEX)) as fh:
                idx = json.load(fh)
        except (OSError, ValueError) as e:
            out["errors"].append(f"{dirpath}: {e}")
            continue
        for key, meta in idx.items():
            out["keys"] += 1
            src = os.path.join(dirpath, meta.get("file") or "")
            row = images.get(key, path)
            if row and row.get("path") and os.path.exists(row["path"]):
                out["already"] += 1
                continue
            if not os.path.isfile(src):
                out["missing_on_nas"] += 1
                continue
            if dry_run:
                out["restored"] += 1
                continue
            dest = images.file_for(key)
            try:
                _write_atomic(src, dest)
                with db._connect(path) as conn:
                    conn.execute("""INSERT OR REPLACE INTO image_record (key, path, url, bytes, fetched_at, source, failed_at)
                        VALUES (?, ?, ?, ?, datetime('now'), ?, NULL)""",
                                 (key, dest, meta.get("url"), os.path.getsize(dest), meta.get("source") or "nas"))
                out["restored"] += 1
            except OSError as e:
                out["errors"].append(f"{key}: {e}")
    logger.info(f"Covers restore: {out['restored']} restored from the NAS, {out['already']} already local, "
                f"{out['missing_on_nas']} listed but missing on the NAS")
    return out
