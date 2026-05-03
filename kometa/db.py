import os
import sqlite3
from contextlib import contextmanager

DB_PATH = "/data/kometa.db"


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
                in_komga          INTEGER NOT NULL DEFAULT 0,
                komga_book_id     TEXT,
                UNIQUE(tracked_series_id, number)
            );

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS match_candidates (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                komga_series_id   TEXT NOT NULL UNIQUE,
                komga_title       TEXT NOT NULL,
                komga_publisher   TEXT,
                komga_year        INTEGER,
                metron_id         INTEGER,
                metron_title      TEXT,
                metron_publisher  TEXT,
                metron_year       INTEGER,
                score             REAL NOT NULL DEFAULT 0,
                confidence        TEXT NOT NULL DEFAULT 'none',
                candidates_json   TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                created_at        TEXT DEFAULT (datetime('now'))
            );
        """)
    _migrate(path)
    _seed_defaults(path)


# --- Push tokens ---

def register_push_token(token: str, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO push_tokens (token) VALUES (?)
            ON CONFLICT(token) DO UPDATE SET updated_at = datetime('now')
        """, (token,))


def get_push_tokens(path=DB_PATH) -> list[str]:
    with _connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT token FROM push_tokens")]


def get_recent_acquisitions(limit: int = 20, path=DB_PATH) -> list[dict]:
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.id, q.issue_number, q.filename, q.updated_at,
                   s.title, s.publisher, s.komga_series_id,
                   i.komga_book_id, i.metron_image
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            LEFT JOIN issue_status i ON i.tracked_series_id = q.tracked_series_id
              AND i.number = q.issue_number
            WHERE q.state = 'done'
            ORDER BY q.updated_at DESC
            LIMIT ?
        """, (limit,))]


def get_pull_list_this_week(path=DB_PATH) -> list[dict]:
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT s.title, s.komga_series_id, s.metron_series_id,
                   i.number, i.store_date, i.in_komga, i.komga_book_id, i.metron_image
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE s.on_pull_list = 1
              AND i.store_date IS NOT NULL
              AND i.store_date >= date('now')
              AND i.store_date <= date('now', '7 days')
            ORDER BY i.store_date, s.title
        """)]


@contextmanager
def _connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
        issue_cols = [r[1] for r in conn.execute("PRAGMA table_info(issue_status)")]
        if "metron_image" not in issue_cols:
            conn.execute("ALTER TABLE issue_status ADD COLUMN metron_image TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_series_id INTEGER NOT NULL REFERENCES tracked_series(id),
                issue_number      REAL NOT NULL,
                state             TEXT NOT NULL DEFAULT 'queued',
                source_url        TEXT,
                filename          TEXT,
                error             TEXT,
                created_at        TEXT DEFAULT (datetime('now')),
                updated_at        TEXT DEFAULT (datetime('now')),
                UNIQUE(tracked_series_id, issue_number)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_tokens (
                token      TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)


def _seed_defaults(path=DB_PATH):
    # Only seed non-secret defaults — credentials come from onboarding
    defaults = {
        "sync_hours": os.environ.get("KOMETA_SYNC_HOURS", "5,12,17"),
    }
    if os.environ.get("CV_API_KEY"):
        defaults["cv_api_key"] = os.environ["CV_API_KEY"]
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


# --- Series ---

def add_series(komga_series_id, metron_series_id, title, publisher=None, year_began=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO tracked_series (komga_series_id, metron_series_id, title, publisher, year_began)
            VALUES (?, ?, ?, ?, ?)
        """, (komga_series_id, metron_series_id, title, publisher, year_began))


def remove_series(series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM issue_status WHERE tracked_series_id = ?", (series_id,))
        conn.execute("DELETE FROM tracked_series WHERE id = ?", (series_id,))


def get_all_series(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tracked_series ORDER BY title")]


def get_series_by_id(series_id, path=DB_PATH):
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        return dict(row) if row else None


def upsert_issue_status(tracked_series_id, number, store_date, in_komga, komga_book_id=None, metron_image=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO issue_status (tracked_series_id, number, store_date, in_komga, komga_book_id, metron_image)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                store_date = excluded.store_date,
                in_komga = excluded.in_komga,
                komga_book_id = excluded.komga_book_id,
                metron_image = excluded.metron_image
        """, (tracked_series_id, number, store_date, int(in_komga), komga_book_id, metron_image))


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


# --- Match candidates ---

def upsert_candidate(komga_series_id, komga_title, komga_publisher, komga_year,
                     metron_id=None, metron_title=None, metron_publisher=None,
                     metron_year=None, score=0, confidence='none',
                     candidates_json=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO match_candidates
              (komga_series_id, komga_title, komga_publisher, komga_year,
               metron_id, metron_title, metron_publisher, metron_year,
               score, confidence, candidates_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(komga_series_id) DO UPDATE SET
              metron_id        = excluded.metron_id,
              metron_title     = excluded.metron_title,
              metron_publisher = excluded.metron_publisher,
              metron_year      = excluded.metron_year,
              score            = excluded.score,
              confidence       = excluded.confidence,
              candidates_json  = excluded.candidates_json,
              status           = 'pending'
        """, (komga_series_id, komga_title, komga_publisher, komga_year,
              metron_id, metron_title, metron_publisher, metron_year,
              score, confidence, candidates_json))


def get_pending_candidates(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM match_candidates WHERE status = 'pending' ORDER BY score DESC, komga_title
        """)]


def get_candidate_komga_ids(path=DB_PATH):
    with _connect(path) as conn:
        rows = conn.execute("SELECT komga_series_id FROM match_candidates").fetchall()
        return {r[0] for r in rows}


def confirm_candidate(komga_series_id, metron_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            UPDATE match_candidates SET status = 'confirmed', metron_id = ?
            WHERE komga_series_id = ?
        """, (metron_id, komga_series_id))


def reject_candidate(komga_series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE match_candidates SET status = 'rejected' WHERE komga_series_id = ?",
            (komga_series_id,)
        )


def get_candidates_summary(path=DB_PATH):
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT confidence, status, COUNT(*) as cnt
            FROM match_candidates GROUP BY confidence, status
        """).fetchall()
        return [dict(r) for r in rows]


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


def get_queue(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.metron_series_id, s.komga_series_id, s.year_began
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            ORDER BY q.updated_at DESC
        """)]


def get_queued_items(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.metron_series_id, s.komga_series_id, s.year_began
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state = 'queued'
            ORDER BY q.created_at ASC
            LIMIT 10
        """)]


def update_queue_state(queue_id, state, source_url=None, filename=None, error=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            UPDATE download_queue SET
                state      = ?,
                source_url = COALESCE(?, source_url),
                filename   = COALESCE(?, filename),
                error      = ?,
                updated_at = datetime('now')
            WHERE id = ?
        """, (state, source_url, filename, error, queue_id))


def remove_queue_item(queue_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))


def get_missing_for_monitored(path=DB_PATH):
    """Return missing released issues for all monitored series not already queued."""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.id as issue_id, i.number, i.store_date,
                   s.id as tracked_series_id, s.title, s.publisher
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.in_komga = 0
              AND (i.store_date IS NULL OR i.store_date <= date('now'))
              AND s.monitor_status = 'monitored'
              AND NOT EXISTS (
                  SELECT 1 FROM download_queue q
                  WHERE q.tracked_series_id = s.id
                    AND q.issue_number = i.number
                    AND q.state NOT IN ('failed', 'not_found')
              )
        """)]


def get_upcoming_issues(days=90, path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT s.title, i.number, i.store_date
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.in_komga = 0
              AND i.store_date IS NOT NULL
              AND i.store_date > date('now')
              AND i.store_date <= date('now', ? || ' days')
              AND s.on_pull_list = 1
            ORDER BY i.store_date, s.title
        """, (str(days),))]
