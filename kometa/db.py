import os
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

# Single source of truth for the DB location. Other modules import this
# (DB_PATH = db.DB_PATH) so the env var gets read exactly once, right here.
DB_PATH = os.environ.get("KOMETA_DB", "/data/kometa.db")


def init_db(path=DB_PATH):
    with _connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracked_series (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                komga_series_id   TEXT NOT NULL UNIQUE,
                metron_series_id  INTEGER NOT NULL,
                title             TEXT NOT NULL,
                publisher         TEXT,
                year_began        INTEGER,
                added_at          TEXT DEFAULT (datetime('now')),
                last_synced       TEXT,
                on_pull_list      INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS issue_status (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_series_id INTEGER NOT NULL REFERENCES tracked_series(id),
                number            REAL NOT NULL,
                store_date        TEXT,
                owned          INTEGER NOT NULL DEFAULT 0,
                komga_book_id     TEXT,
                UNIQUE(tracked_series_id, number)
            );

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issue_details_cache (
                locg_issue_id TEXT PRIMARY KEY,
                data_json     TEXT NOT NULL,
                fetched_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS creator_works_cache (
                people_id  TEXT PRIMARY KEY,
                data_json  TEXT NOT NULL,
                fetched_at TEXT DEFAULT (datetime('now'))
            );

        """)
    _migrate(path)
    _seed_defaults(path)


@contextmanager
def _connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_connection(path=DB_PATH):
    return _connect(path)


def _migrate(path=DB_PATH):
    with _connect(path) as conn:
        series_cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
        if "on_pull_list" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN on_pull_list INTEGER NOT NULL DEFAULT 1")
        if "monitor_status" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN monitor_status TEXT NOT NULL DEFAULT 'monitored'")
        if "folder_path" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN folder_path TEXT")

        if "cv_volume_id" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN cv_volume_id TEXT")
        if "locg_series_id" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN locg_series_id INTEGER")

        # Make komga_series_id nullable — SQLite can't drop NOT NULL via ALTER, must rebuild
        series_info = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(tracked_series)")}
        if series_info.get("komga_series_id", 0) == 1:
            conn.execute("ALTER TABLE tracked_series RENAME TO _ts_old")
            conn.execute("""
                CREATE TABLE tracked_series (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    komga_series_id   TEXT UNIQUE,
                    metron_series_id  INTEGER NOT NULL,
                    title             TEXT NOT NULL,
                    publisher         TEXT,
                    year_began        INTEGER,
                    added_at          TEXT DEFAULT (datetime('now')),
                    last_synced       TEXT,
                    on_pull_list      INTEGER NOT NULL DEFAULT 1,
                    monitor_status    TEXT NOT NULL DEFAULT 'monitored',
                    folder_path       TEXT,
                    cv_volume_id      TEXT,
                    locg_series_id    INTEGER
                )
            """)
            conn.execute("""
                INSERT INTO tracked_series
                    (id, komga_series_id, metron_series_id, title, publisher, year_began,
                     added_at, last_synced, on_pull_list, monitor_status, folder_path,
                     cv_volume_id, locg_series_id)
                SELECT id, komga_series_id, metron_series_id, title, publisher, year_began,
                       added_at, last_synced, on_pull_list, monitor_status, folder_path,
                       cv_volume_id, locg_series_id
                FROM _ts_old
            """)
            conn.execute("DROP TABLE _ts_old")

        # Make metron_series_id nullable — fetch fresh PRAGMA after any prior rebuild
        series_info2 = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(tracked_series)")}
        if series_info2.get("metron_series_id", 0) == 1:
            conn.execute("ALTER TABLE tracked_series RENAME TO _ts_old2")
            conn.execute("""
                CREATE TABLE tracked_series (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    komga_series_id   TEXT UNIQUE,
                    metron_series_id  INTEGER,
                    title             TEXT NOT NULL,
                    publisher         TEXT,
                    year_began        INTEGER,
                    added_at          TEXT DEFAULT (datetime('now')),
                    last_synced       TEXT,
                    on_pull_list      INTEGER NOT NULL DEFAULT 1,
                    monitor_status    TEXT NOT NULL DEFAULT 'monitored',
                    folder_path       TEXT,
                    cv_volume_id      TEXT,
                    locg_series_id    INTEGER
                )
            """)
            conn.execute("""
                INSERT INTO tracked_series
                    (id, komga_series_id, metron_series_id, title, publisher, year_began,
                     added_at, last_synced, on_pull_list, monitor_status, folder_path,
                     cv_volume_id, locg_series_id)
                SELECT id, komga_series_id, metron_series_id, title, publisher, year_began,
                       added_at, last_synced, on_pull_list, monitor_status, folder_path,
                       cv_volume_id, locg_series_id
                FROM _ts_old2
            """)
            conn.execute("DROP TABLE _ts_old2")

        issue_cols = [r[1] for r in conn.execute("PRAGMA table_info(issue_status)")]
        # Rename the old in_komga column to owned — it always meant "owned on disk",
        # never "present in Komga". Idempotent: only fires on DBs predating the rename.
        if "in_komga" in issue_cols and "owned" not in issue_cols:
            conn.execute("ALTER TABLE issue_status RENAME COLUMN in_komga TO owned")
            issue_cols = [r[1] for r in conn.execute("PRAGMA table_info(issue_status)")]
        if "metron_image" not in issue_cols:
            conn.execute("ALTER TABLE issue_status ADD COLUMN metron_image TEXT")
        if "metron_issue_id" not in issue_cols:
            conn.execute("ALTER TABLE issue_status ADD COLUMN metron_issue_id INTEGER")
        if "locg_issue_id" not in issue_cols:
            conn.execute("ALTER TABLE issue_status ADD COLUMN locg_issue_id TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS variant_prefs (
                tracked_series_id INTEGER NOT NULL REFERENCES tracked_series(id),
                number            REAL NOT NULL,
                selected          TEXT NOT NULL,
                primary_id        TEXT NOT NULL,
                UNIQUE(tracked_series_id, number)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_series_id INTEGER NOT NULL REFERENCES tracked_series(id),
                issue_number      REAL NOT NULL,
                state             TEXT NOT NULL DEFAULT 'queued',
                source_url        TEXT,
                filename          TEXT,
                error             TEXT,
                retry_after       TEXT,
                created_at        TEXT DEFAULT (datetime('now')),
                updated_at        TEXT DEFAULT (datetime('now')),
                UNIQUE(tracked_series_id, issue_number)
            )
        """)
        dq_cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue)")]
        if "retry_after" not in dq_cols:
            conn.execute("ALTER TABLE download_queue ADD COLUMN retry_after TEXT")
        if "sab_nzo_id" not in dq_cols:
            conn.execute("ALTER TABLE download_queue ADD COLUMN sab_nzo_id TEXT")


# config key -> env var for first-boot provisioning. Lets a deployer configure
# entirely from compose/env instead of clicking through Settings. Seeded with
# INSERT OR IGNORE (below), so it's first-run only — the UI stays the source of
# truth after that, and changing the env later won't clobber UI edits.
_ENV_SEEDED_CONFIG = {
    "comics_root":      "COMICS_ROOT",
    "cv_api_key":       "CV_API_KEY",
    "komga_url":        "KOMGA_URL",
    "komga_user":       "KOMGA_USER",
    "komga_pass":       "KOMGA_PASS",
    "komga_library_id": "KOMGA_LIBRARY_ID",
    "metron_user":      "METRON_USER",
    "metron_pass":      "METRON_PASS",
}


def _seed_defaults(path=DB_PATH):
    defaults = {
        "sync_hours": os.environ.get("KOMETA_SYNC_HOURS", "5,12,17"),
    }
    # Seed optional integrations from env only when actually provided, so we don't
    # write empty rows that masquerade as "configured".
    for cfg_key, env_var in _ENV_SEEDED_CONFIG.items():
        val = os.environ.get(env_var)
        if val:
            defaults[cfg_key] = val
    with _connect(path) as conn:
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_config(path=DB_PATH) -> dict:
    with _connect(path) as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_config(updates: dict, path=DB_PATH):
    with _connect(path) as conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


# --- Issue details cache (LOCG desc + credits; also the recommendation signal) ---

def get_issue_details_cache(locg_issue_id, path=DB_PATH):
    import json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT data_json FROM issue_details_cache WHERE locg_issue_id = ?",
            (str(locg_issue_id),),
        ).fetchone()
        return json.loads(row["data_json"]) if row else None


def set_issue_details_cache(locg_issue_id, data, path=DB_PATH):
    import json
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO issue_details_cache (locg_issue_id, data_json, fetched_at) "
            "VALUES (?, ?, datetime('now'))",
            (str(locg_issue_id), json.dumps(data)),
        )


def get_creator_works_cache(people_id, path=DB_PATH):
    import json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT data_json FROM creator_works_cache WHERE people_id = ?",
            (str(people_id),),
        ).fetchone()
        return json.loads(row["data_json"]) if row else None


def set_creator_works_cache(people_id, data, path=DB_PATH):
    import json
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO creator_works_cache (people_id, data_json, fetched_at) "
            "VALUES (?, ?, datetime('now'))",
            (str(people_id), json.dumps(data)),
        )


# --- Series ---

def add_series(komga_series_id=None, metron_series_id=None, title=None, publisher=None,
               year_began=None, folder_path=None, on_pull_list=True, locg_series_id=None,
               path=DB_PATH) -> int:
    with _connect(path) as conn:
        cur = conn.execute("""
            INSERT INTO tracked_series (komga_series_id, metron_series_id, title, publisher,
                                        year_began, folder_path, on_pull_list, locg_series_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (komga_series_id, metron_series_id, title, publisher, year_began,
              folder_path, int(on_pull_list), locg_series_id))
        return cur.lastrowid


def remove_series(series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM issue_status WHERE tracked_series_id = ?", (series_id,))
        conn.execute("DELETE FROM tracked_series WHERE id = ?", (series_id,))


def get_all_series(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tracked_series ORDER BY title")]


def get_all_series_summaries(path=DB_PATH):
    """Single-query bulk aggregation of issue counts for all series."""
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT
                tracked_series_id,
                SUM(CASE WHEN owned = 1 THEN 1 ELSE 0 END) as owned,
                SUM(CASE WHEN owned = 0 AND (store_date IS NULL OR store_date < ?) THEN 1 ELSE 0 END) as missing,
                SUM(CASE WHEN owned = 0 AND store_date IS NOT NULL AND store_date >= ? THEN 1 ELSE 0 END) as upcoming,
                MIN(CASE WHEN owned = 0 AND store_date IS NOT NULL AND store_date >= ? AND store_date <= ? THEN store_date END) as next_release,
                (SELECT metron_image FROM issue_status i2
                 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.owned = 0
                   AND i2.store_date IS NOT NULL
                   AND i2.store_date >= ?
                   AND i2.store_date <= ?
                   AND i2.metron_image IS NOT NULL
                 ORDER BY i2.store_date ASC LIMIT 1) as next_release_image
            FROM issue_status
            GROUP BY tracked_series_id
        """, (today, today, today, cutoff, today, cutoff))
        return {r["tracked_series_id"]: dict(r) for r in rows}


def get_series_by_id(series_id, path=DB_PATH):
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        return dict(row) if row else None


def upsert_issue_status(tracked_series_id, number, store_date, owned, komga_book_id=None, metron_image=None, metron_issue_id=None, locg_issue_id=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO issue_status (tracked_series_id, number, store_date, owned, komga_book_id, metron_image, metron_issue_id, locg_issue_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                store_date      = COALESCE(excluded.store_date, store_date),
                owned        = excluded.owned,
                komga_book_id   = excluded.komga_book_id,
                metron_image    = excluded.metron_image,
                metron_issue_id = COALESCE(excluded.metron_issue_id, metron_issue_id),
                locg_issue_id   = COALESCE(excluded.locg_issue_id, locg_issue_id)
        """, (tracked_series_id, number, store_date, int(owned), komga_book_id, metron_image, metron_issue_id, locg_issue_id))


def set_pull_list(series_id, on_pull_list, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET on_pull_list = ? WHERE id = ?",
            (int(on_pull_list), series_id),
        )


def mark_synced(series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            UPDATE tracked_series SET last_synced = datetime('now') WHERE id = ?
        """, (series_id,))


def get_issues_for_series(tracked_series_id, path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM issue_status WHERE tracked_series_id = ? ORDER BY number
        """, (tracked_series_id,))]


def set_folder_path(series_id, folder_path, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET folder_path = ? WHERE id = ?",
            (folder_path, series_id),
        )


def set_cv_volume_id(series_id, cv_volume_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET cv_volume_id = ? WHERE id = ?",
            (str(cv_volume_id), series_id),
        )


def set_locg_series_id(series_id, locg_series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET locg_series_id = ? WHERE id = ?",
            (locg_series_id, series_id),
        )


def set_monitor_status(series_id, status, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET monitor_status = ? WHERE id = ?",
            (status, series_id),
        )


# --- Download queue ---

def queue_issue(tracked_series_id, issue_number, path=DB_PATH):
    """Add to queue; re-queues failed/not_found items but skips in-progress/done."""
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO download_queue (tracked_series_id, issue_number, state)
            VALUES (?, ?, 'queued')
            ON CONFLICT(tracked_series_id, issue_number) DO UPDATE SET
                state      = CASE WHEN state IN ('failed', 'not_found') THEN 'queued' ELSE state END,
                error      = CASE WHEN state IN ('failed', 'not_found') THEN NULL ELSE error END,
                updated_at = datetime('now')
        """, (tracked_series_id, issue_number))


def queue_pack(tracked_series_id, nzo_id: str, nzb_url: str, path=DB_PATH):
    """Insert a pack sentinel (issue_number=-1) directly into pending_usenet state."""
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO download_queue (tracked_series_id, issue_number, state, sab_nzo_id, source_url)
            VALUES (?, -1, 'pending_usenet', ?, ?)
            ON CONFLICT(tracked_series_id, issue_number) DO UPDATE SET
                state      = CASE WHEN state IN ('failed', 'not_found', 'done') THEN 'pending_usenet' ELSE state END,
                sab_nzo_id = CASE WHEN state IN ('failed', 'not_found', 'done') THEN excluded.sab_nzo_id ELSE sab_nzo_id END,
                source_url = CASE WHEN state IN ('failed', 'not_found', 'done') THEN excluded.source_url ELSE source_url END,
                updated_at = datetime('now')
        """, (tracked_series_id, nzo_id, nzb_url))


def get_queue(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.metron_series_id, s.komga_series_id, s.year_began
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            ORDER BY q.updated_at DESC
        """)]


def reset_stuck_queue_items(path=DB_PATH):
    """Reset searching/downloading items left orphaned by a container restart."""
    with _connect(path) as conn:
        conn.execute("""
            UPDATE download_queue
            SET state = 'queued', error = NULL, sab_nzo_id = NULL, updated_at = datetime('now')
            WHERE state IN ('searching', 'downloading')
        """)


def get_queued_items(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.metron_series_id, s.komga_series_id, s.year_began, s.folder_path
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state = 'queued'
              AND (q.retry_after IS NULL OR q.retry_after <= datetime('now'))
            ORDER BY q.created_at ASC
            LIMIT 10
        """)]


def get_pending_usenet_items(path=DB_PATH):
    """Items waiting on SABnzbd to finish downloading."""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.year_began, s.folder_path
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state = 'pending_usenet' AND q.sab_nzo_id IS NOT NULL
        """)]


def set_sab_nzo_id(queue_id, nzo_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET sab_nzo_id = ?, updated_at = datetime('now') WHERE id = ?",
            (nzo_id, queue_id),
        )


def update_queue_state(queue_id, state, source_url=None, filename=None, error=None, retry_after=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            UPDATE download_queue SET
                state       = ?,
                source_url  = COALESCE(?, source_url),
                filename    = COALESCE(?, filename),
                error       = ?,
                retry_after = ?,
                updated_at  = datetime('now')
            WHERE id = ?
        """, (state, source_url, filename, error, retry_after, queue_id))


def complete_download(queue_id, tracked_series_id, issue_number, store_date,
                      filename, set_folder_path=None, path=DB_PATH):
    """Mark a queued download done AND record issue ownership in ONE transaction.

    The whole point is the single commit. Split these across two transactions —
    like the old code did — and a crash in the gap leaves the file rotting on disk
    while the DB still swears the issue is missing. Next sync sees the hole and
    downloads the damn thing all over again. One commit, no gap, no ghost re-download.

    set_folder_path: when given, also stamp the series' folder_path (callers pass it
    only when the series doesn't have one yet — same condition as before, just atomic).
    """
    with _connect(path) as conn:
        conn.execute("""
            UPDATE download_queue SET
                state       = 'done',
                filename    = COALESCE(?, filename),
                error       = NULL,
                retry_after = NULL,
                updated_at  = datetime('now')
            WHERE id = ?
        """, (filename, queue_id))
        if set_folder_path is not None:
            conn.execute(
                "UPDATE tracked_series SET folder_path = ? WHERE id = ?",
                (set_folder_path, tracked_series_id),
            )
        conn.execute("""
            INSERT INTO issue_status (tracked_series_id, number, store_date, owned,
                                      komga_book_id, metron_image, metron_issue_id, locg_issue_id)
            VALUES (?, ?, ?, 1, NULL, NULL, NULL, NULL)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                store_date      = COALESCE(excluded.store_date, store_date),
                owned        = excluded.owned,
                komga_book_id   = excluded.komga_book_id,
                metron_image    = excluded.metron_image,
                metron_issue_id = COALESCE(excluded.metron_issue_id, metron_issue_id),
                locg_issue_id   = COALESCE(excluded.locg_issue_id, locg_issue_id)
        """, (tracked_series_id, issue_number, store_date))


def remove_queue_item(queue_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))


def clear_queue_history(path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM download_queue WHERE state IN ('done', 'not_found', 'failed')")


def get_missing_counts_by_series(path=DB_PATH) -> dict[int, int]:
    """Return {series_id: count} of released issues not yet in Komga, for all monitored series."""
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT s.id, COUNT(*) as cnt
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.owned = 0
              AND (i.store_date IS NULL OR i.store_date <= date('now'))
              AND s.monitor_status = 'monitored'
              AND s.on_pull_list = 1
            GROUP BY s.id
        """)
        return {r["id"]: r["cnt"] for r in rows}


def has_active_pack(series_id, path=DB_PATH) -> bool:
    """True if a pack queue entry exists for this series that isn't done or failed."""
    with _connect(path) as conn:
        row = conn.execute("""
            SELECT 1 FROM download_queue
            WHERE tracked_series_id = ? AND issue_number = -1
              AND state NOT IN ('done', 'failed', 'not_found')
            LIMIT 1
        """, (series_id,)).fetchone()
        return row is not None


def get_missing_for_monitored(path=DB_PATH):
    """Return missing released issues for all monitored series not already queued."""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.id as issue_id, i.number, i.store_date,
                   s.id as tracked_series_id, s.title, s.publisher
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.owned = 0
              AND (i.store_date IS NULL OR i.store_date <= date('now'))
              AND s.on_pull_list = 1
              AND NOT EXISTS (
                  SELECT 1 FROM download_queue q
                  WHERE q.tracked_series_id = s.id
                    AND q.issue_number = i.number
                    AND q.state NOT IN ('failed', 'not_found')
              )
        """)]


def get_upcoming_issues(days=90, past=0, path=DB_PATH):
    if past:
        lookback = f"date('now', '-{int(past)} days')"
    else:
        lookback = "date('now', '-7 days', 'weekday 0')"
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute(f"""
            SELECT s.id, s.title, i.number, i.store_date, i.owned
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.store_date IS NOT NULL
              AND i.store_date >= {lookback}
              AND i.store_date <= date('now', ? || ' days')
              AND s.on_pull_list = 1
            ORDER BY i.store_date, s.title
        """, (str(days),))]


def set_variant_prefs(tracked_series_id, number, selected: list, primary_id: str, path=DB_PATH):
    import json
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO variant_prefs (tracked_series_id, number, selected, primary_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                selected   = excluded.selected,
                primary_id = excluded.primary_id
        """, (tracked_series_id, number, json.dumps(selected), primary_id))


def get_variant_prefs(tracked_series_id, number, path=DB_PATH):
    import json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT selected, primary_id FROM variant_prefs WHERE tracked_series_id = ? AND number = ?",
            (tracked_series_id, number)
        ).fetchone()
    if not row:
        return None
    return {"selected": json.loads(row["selected"]), "primary_id": row["primary_id"]}


def clear_variant_prefs(tracked_series_id, number, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM variant_prefs WHERE tracked_series_id = ? AND number = ?",
            (tracked_series_id, number)
        )
