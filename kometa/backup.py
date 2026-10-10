"""Nightly backup of the record: the SQLite database and the cover store.

Everything Kometa has learned — the local catalogue record, progress, marks,
reading lists, the image index — lives in one SQLite file, and the covers
beside it; until 2026-10-10 the only copies were four hand-made ones from June.
This writes a consistent copy of the database with SQLite's own backup call
(safe while the app is running) and mirrors the cover store, file by file, to a
folder on the NAS share the container already mounts: `_kometa-backup` beside
the comics, which the shelf scanner skips like every underscore folder.
Covers are immutable by key, so the mirror copies only what's missing or a
different size and deletes nothing. The last BACKUP_KEEP database copies stay.
"""
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
BACKUP_KEEP = 7
BACKUP_DIR_NAME = "_kometa-backup"
_state = {"running": False}


def default_dest() -> str:
    from kometa import sources
    return os.path.join(sources.comics_root(), BACKUP_DIR_NAME)


def _copy_db(src_path: str, dest_dir: str, stamp: str) -> tuple[str, int]:
    """A consistent copy via the backup API, written to a temp name and renamed."""
    final = os.path.join(dest_dir, f"kometa-{stamp}.db")
    tmp = final + ".part"
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, final)
    return final, os.path.getsize(final)


def _prune(dest_dir: str, keep: int) -> int:
    copies = sorted(f for f in os.listdir(dest_dir) if f.startswith("kometa-") and f.endswith(".db"))
    gone = 0
    for f in copies[:-keep] if len(copies) > keep else []:
        try:
            os.remove(os.path.join(dest_dir, f)); gone += 1
        except OSError:
            pass
    return gone


def _mirror(src_root: str, dest_root: str) -> tuple[int, int]:
    """Copy files missing from dest or of a different size. Deletes nothing."""
    copied, bytes_ = 0, 0
    if not os.path.isdir(src_root):
        return 0, 0
    for dirpath, _, files in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        out_dir = os.path.join(dest_root, rel) if rel != "." else dest_root
        os.makedirs(out_dir, exist_ok=True)
        for f in files:
            if f.endswith(".part"):
                continue
            s = os.path.join(dirpath, f)
            d = os.path.join(out_dir, f)
            try:
                size = os.path.getsize(s)
                if os.path.exists(d) and os.path.getsize(d) == size:
                    continue
                shutil.copyfile(s, d + ".part")
                os.replace(d + ".part", d)
                copied += 1
                bytes_ += size
            except OSError as e:
                logger.info(f"Backup: {s} skipped: {e}")
    return copied, bytes_


def backup_now(dest_root: str | None = None, db_path: str | None = None, covers_dir: str | None = None,
               keep: int = BACKUP_KEEP) -> dict:
    """Run a backup. Returns what was written; also remembered in config as backup_last."""
    db_path = db_path or DB_PATH
    dest_root = dest_root or default_dest()
    if covers_dir is None:
        from kometa.images import COVERS_DIR
        covers_dir = COVERS_DIR
    t0 = time.time()
    os.makedirs(dest_root, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    out = {"dest": dest_root, "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        path, size = _copy_db(db_path, dest_root, stamp)
        out.update(db_file=os.path.basename(path), db_bytes=size)
        out["pruned"] = _prune(dest_root, keep)
        copied, cbytes = _mirror(covers_dir, os.path.join(dest_root, "covers"))
        out.update(covers_copied=copied, covers_bytes=cbytes)
        out["ok"] = True
    except Exception as e:
        out.update(ok=False, error=str(e))
        logger.warning(f"Backup failed: {e}")
    out["seconds"] = round(time.time() - t0, 1)
    try:
        db.set_config({"backup_last": json.dumps(out)}, db_path)
    except Exception as e:
        logger.info(f"Backup: couldn't record the run: {e}")
    logger.info(f"Backup: {out}")
    return out


def last(db_path: str | None = None) -> dict:
    try:
        raw = db.get_config(db_path or DB_PATH).get("backup_last")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def status() -> dict:
    return {"running": _state["running"], "dest": default_dest(), "last": last()}


def run_in_background() -> bool:
    if _state["running"]:
        return False
    _state["running"] = True

    def run():
        try:
            backup_now()
        finally:
            _state["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def nightly() -> None:
    """The scheduler's entry: one run, logged, never raising into apscheduler."""
    if _state["running"]:
        return
    _state["running"] = True
    try:
        backup_now()
    finally:
        _state["running"] = False
