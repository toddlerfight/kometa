import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

# Single source of truth for the DB location. Other modules import this
# (DB_PATH = db.DB_PATH) so the env var gets read exactly once, right here.
DB_PATH = os.environ.get("KOMETA_DB", "/data/kometa.db")


def init_db(path=DB_PATH):
    with _connect(path) as conn:
        # WAL is a persistent property of the DB file — set once here, not on
        # every _connect (it was costing a pragma round-trip per DB call).
        conn.execute("PRAGMA journal_mode=WAL")
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

            -- A Komga book id outlives the file behind it: replace a coverless
            -- #015 with a whole one at the same path and Komga keeps the id, so
            -- '/api/book/<id>/thumbnail' kept serving the old cover — from our
            -- disk cache, and from every browser told to hold it for 30 days.
            -- The version (file mtime + size, as Komga reports them) rides on
            -- the URL and the cache key, so a new file is a new cover.
            CREATE TABLE IF NOT EXISTS komga_book_version (
                book_id TEXT PRIMARY KEY,
                v       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades_cache (
                tracked_series_id INTEGER PRIMARY KEY REFERENCES tracked_series(id),
                data_json         TEXT NOT NULL,
                fetched_at        TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS arc_discovery_cache (
                tracked_series_id INTEGER PRIMARY KEY REFERENCES tracked_series(id),
                data_json         TEXT NOT NULL,
                fetched_at        TEXT DEFAULT (datetime('now'))
            );

            -- The reader's shelf: one row per comic FILE. Registered lazily the first
            -- time a book is opened (the whole-shelf index fills it in bulk later).
            -- pages_json = [[entry_name, width, height], ...] in reading order.
            -- size+mtime fingerprint the file: change either and the page list is
            -- rebuilt and every cached page for the old version is ignored.
            CREATE TABLE IF NOT EXISTS books (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                path              TEXT NOT NULL UNIQUE,
                size              INTEGER,
                mtime             REAL,
                page_count        INTEGER,
                pages_json        TEXT,
                tracked_series_id INTEGER,
                number            REAL,
                added_at          TEXT DEFAULT (datetime('now'))
            );

            -- Read progress, per READER. One reader ('me') today; the key is there so
            -- a second one is a data change, not a schema change. updated_at is the
            -- CLIENT's clock: a write older than what's stored loses (offline
            -- devices replaying a queue must not drag progress backwards).
            -- The whole shelf, one row per series FOLDER (publisher/series), tracked
            -- or not. Tracked series link through tracked_series_id; untracked
            -- ones exist only here, so acquisition (sync, sweeps, LOCG) never sees
            -- them. Rebuilt by the shelf scan; scanned_at marks the last sighting.
            CREATE TABLE IF NOT EXISTS shelf_series (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path       TEXT NOT NULL UNIQUE,
                title             TEXT NOT NULL,
                publisher         TEXT,
                tracked_series_id INTEGER,
                book_count        INTEGER NOT NULL DEFAULT 0,
                scanned_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS read_progress (
                reader_id  TEXT NOT NULL,
                book_id    INTEGER NOT NULL REFERENCES books(id),
                page       INTEGER NOT NULL,
                completed  INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (reader_id, book_id)
            );

        """)
    _migrate(path)
    _seed_defaults(path)


@contextmanager
def _connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
        # Story arcs ride the tracked_series machinery (folder, trades, queue,
        # Komga link) as kind='arc'; their cross-title reading order lives in the
        # dedicated arc_issues table (issue_status is single-title, can't hold it).
        # NB: the kind/cv_arc_id COLUMNS are added AFTER the nullable-rebuilds below
        # — those recreate tracked_series and would drop anything added up here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS arc_issues (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                arc_series_id   INTEGER NOT NULL REFERENCES tracked_series(id),
                reading_order   INTEGER NOT NULL,
                source_title    TEXT NOT NULL,
                number          TEXT,
                story_title     TEXT,
                cv_issue_id     TEXT,
                cv_volume_id    TEXT,
                image_url       TEXT,
                komga_book_id   TEXT,
                owned           INTEGER NOT NULL DEFAULT 0,
                UNIQUE(arc_series_id, reading_order)
            )
        """)

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

        # Arc columns go in AFTER both nullable-rebuilds above (they recreate
        # tracked_series from the old column set, dropping anything added earlier).
        # Fresh PRAGMA so this lands whatever the rebuilds left behind.
        ts_cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
        if "kind" not in ts_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN kind TEXT NOT NULL DEFAULT 'series'")
        if "cv_arc_id" not in ts_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN cv_arc_id TEXT")
        if "cv_volume_id" not in ts_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN cv_volume_id TEXT")
        # Per-series single-issue page ceiling. NULL = the global default (70).
        # Exists because Head Lopper is a 72-page quarterly and the webtoon guard
        # kept executing it for the crime of being exactly what it says it is.
        if "page_max" not in ts_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN page_max INTEGER")

        # arc_issues gained cv_volume_id (the authoritative CV volume per issue, for
        # routing each issue to the right tracked run — e.g. Batman 1940, not 2016).
        ai_cols = [r[1] for r in conn.execute("PRAGMA table_info(arc_issues)")]
        if "cv_volume_id" not in ai_cols:
            conn.execute("ALTER TABLE arc_issues ADD COLUMN cv_volume_id TEXT")
        # arc_issues gained image_url (CV per-issue cover, so reading-order tiles show
        # real art instead of empty boxes — same batch call that resolves number/volume).
        if "image_url" not in ai_cols:
            conn.execute("ALTER TABLE arc_issues ADD COLUMN image_url TEXT")

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
        # How a series got its LOCG link. NULL = added on purpose (wizard etc.);
        # 'auto' = the shelf importer matched it confidently; 'needs_match' = the
        # importer couldn't be sure, so sync must NOT guess by title — you pick.
        series_cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
        if "match_status" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN match_status TEXT")
        # When the LOCG issue list was last fetched — the whole point being NOT to
        # refetch a finished run's unchanging list three times a day.
        if "locg_fetched_at" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN locg_fetched_at TEXT")
        if "metron_fetched_at" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN metron_fetched_at TEXT")
        # Outcome of linking an already-tracked series to Metron: 'linked' / 'none'
        # (Metron couldn't place it confidently — don't ask again every tick).
        if "metron_link" not in series_cols:
            conn.execute("ALTER TABLE tracked_series ADD COLUMN metron_link TEXT")
        if "metron_type" not in series_cols:
            # Metron's series type (One-Shot / Limited / Trade Paperback / …) —
            # what tells a one-file folder apart from a stub (kometa/singles.py)
            conn.execute("ALTER TABLE tracked_series ADD COLUMN metron_type TEXT")
        if "from_list_id" not in series_cols:
            # the reading list whose 'Get' tracked this run (pull list off): the
            # series page says why it exists, and the Library counts it honestly
            conn.execute("ALTER TABLE tracked_series ADD COLUMN from_list_id INTEGER")
        if "locg_link" not in series_cols:
            # mirror of metron_link: a Metron-matched series tried against LOCG
            # ('linked' / 'none') so the trickle doesn't ask twice
            conn.execute("ALTER TABLE tracked_series ADD COLUMN locg_link TEXT")
        book_cols = [r[1] for r in conn.execute("PRAGMA table_info(books)")]
        if book_cols and "shelf_series_id" not in book_cols:
            conn.execute("ALTER TABLE books ADD COLUMN shelf_series_id INTEGER")
        rp_cols = [r[1] for r in conn.execute("PRAGMA table_info(read_progress)")]
        if "dismissed" not in rp_cols:
            # On Deck 'not now': hidden from Continue reading, place kept; any
            # new progress write clears it (docs/reader-spec.md)
            conn.execute("ALTER TABLE read_progress ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
        # An issue you've told Kometa to stop chasing — a #0 preview that never got a
        # real release, say. Not owned, never 'missing': sweeps, search-missing and the
        # counts all skip it. Sync's upserts don't name this column, so it survives them.
        if "ignored" not in issue_cols:
            conn.execute("ALTER TABLE issue_status ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")
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
        # Releases that failed DELIVERY for an ISSUE, not for a queue row. The
        # row-level failed_sources list dies with the row — Deadly Class #47's
        # second row bought the exact same no-pages NZB its first row had already
        # condemned. This outlives re-queues, retries and Clear history.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_releases (
                tracked_series_id INTEGER NOT NULL,
                issue_number      REAL NOT NULL,
                url               TEXT NOT NULL,
                failed_at         TEXT DEFAULT (datetime('now')),
                UNIQUE(tracked_series_id, issue_number, url)
            )
        """)
        dq_cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue)")]
        if "retry_after" not in dq_cols:
            conn.execute("ALTER TABLE download_queue ADD COLUMN retry_after TEXT")
        if "sab_nzo_id" not in dq_cols:
            conn.execute("ALTER TABLE download_queue ADD COLUMN sab_nzo_id TEXT")
        if "rl_attempts" not in dq_cols:
            # consecutive rate-limit hits for this job — caps auto-retry so a
            # hard ban eventually fails for real instead of looping forever
            conn.execute("ALTER TABLE download_queue ADD COLUMN rl_attempts INTEGER DEFAULT 0")
        if "kind" not in dq_cols:
            # Generalize the queue: a row is a Kometa acquisition, not just an issue.
            # issue_number becomes nullable, kind/locg_id/meta_json carry the trade
            # case, and the unique key goes kind-aware (partial indexes). One-time
            # rebuild — old rows copy straight over as kind='issue'.
            conn.executescript("""
                CREATE TABLE download_queue_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracked_series_id INTEGER NOT NULL REFERENCES tracked_series(id),
                    kind              TEXT NOT NULL DEFAULT 'issue',
                    issue_number      REAL,
                    locg_id           TEXT,
                    meta_json         TEXT,
                    state             TEXT NOT NULL DEFAULT 'queued',
                    source_url        TEXT,
                    filename          TEXT,
                    error             TEXT,
                    retry_after       TEXT,
                    sab_nzo_id        TEXT,
                    rl_attempts       INTEGER DEFAULT 0,
                    created_at        TEXT DEFAULT (datetime('now')),
                    updated_at        TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO download_queue_new
                    (id, tracked_series_id, kind, issue_number, state, source_url,
                     filename, error, retry_after, sab_nzo_id, rl_attempts, created_at, updated_at)
                    SELECT id, tracked_series_id, 'issue', issue_number, state, source_url,
                     filename, error, retry_after, sab_nzo_id, rl_attempts, created_at, updated_at
                    FROM download_queue;
                DROP TABLE download_queue;
                ALTER TABLE download_queue_new RENAME TO download_queue;
                -- Full (non-partial) unique indexes so the ON CONFLICT upserts in
                -- queue_issue/queue_trade match them. NULLs are distinct in SQLite,
                -- so an issue (locg_id NULL) and a trade (issue_number NULL) never
                -- collide, and many trades per series each keep a NULL issue_number.
                CREATE UNIQUE INDEX idx_dq_issue ON download_queue(tracked_series_id, issue_number);
                CREATE UNIQUE INDEX idx_dq_trade ON download_queue(tracked_series_id, locg_id);
            """)

        # torrent_hash: opaque qBittorrent handle for a pending_torrent job — the
        # twin of sab_nzo_id for the torrent path. Fresh PRAGMA read so it lands
        # whether or not the table-rebuild above just ran.
        dq_cols2 = [r[1] for r in conn.execute("PRAGMA table_info(download_queue)")]
        if "torrent_hash" not in dq_cols2:
            conn.execute("ALTER TABLE download_queue ADD COLUMN torrent_hash TEXT")
        if "failed_channels" not in dq_cols2:
            # JSON list of CHANNELS ('usenet'/'torrent') whose DELIVERY failed for
            # this row. A channel that already proved it can't deliver gets benched
            # on the next attempt — the cascade starts at the next rung instead of
            # re-buying a SAB/qBit cycle. GetComics is never benched (last rung,
            # transient failures).
            conn.execute("ALTER TABLE download_queue ADD COLUMN failed_channels TEXT")
        if "failed_sources" not in dq_cols2:
            # JSON list of release URLs (NZB/magnet) that FAILED DELIVERY for this
            # row — retention-rotted NZBs, dead torrents. Retries feed these to the
            # searches as exclusions so a reattempt tries the next-best release
            # instead of re-buying the same corpse. GetComics links are deliberately
            # NOT recorded: their failures (host 403 walls) are transient and the
            # same link often works an hour later.
            conn.execute("ALTER TABLE download_queue ADD COLUMN failed_sources TEXT")

        # Indexes matching the actual hot query shapes: queue pollers filter on
        # state; the backdrop/upcoming/missing queries filter+order issue_status
        # on (series, store_date); arc discovery looks series up by CV volume.
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_dq_state ON download_queue(state);
            CREATE INDEX IF NOT EXISTS idx_is_series_date ON issue_status(tracked_series_id, store_date);
            CREATE INDEX IF NOT EXISTS idx_ts_cv_volume ON tracked_series(cv_volume_id);
        """)


# config key -> env var for first-boot provisioning. Lets a deployer configure
# entirely from compose/env instead of clicking through Settings. Seeded with
# INSERT OR IGNORE (below), so it's first-run only — the UI stays the source of
# truth after that, and changing the env later won't clobber UI edits.
_ENV_SEEDED_CONFIG = {
    "comics_root":      "COMICS_ROOT",
    "komga_url":        "KOMGA_URL",
    "komga_user":       "KOMGA_USER",
    "komga_pass":       "KOMGA_PASS",
    "komga_library_id": "KOMGA_LIBRARY_ID",
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
    _config_cache.pop(path, None)


# get_config is called by EVERY sources.py accessor — several times per queue
# item, once per thumbnail request — and was opening a fresh connection + full
# table scan each time. The table only changes through set_config/_seed_defaults
# (both below), so an invalidate-on-write cache is exact, not best-effort.
_config_cache: dict = {}


def get_config(path=DB_PATH) -> dict:
    cached = _config_cache.get(path)
    if cached is not None:
        return dict(cached)  # copy — callers mutate their view, not the cache
    with _connect(path) as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
    _config_cache[path] = cfg
    return dict(cfg)


def set_config(updates: dict, path=DB_PATH):
    with _connect(path) as conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    _config_cache.pop(path, None)


# --- Issue details cache (LOCG desc + credits for the Details tab; the external
#     kometa-recommend project also reads this cache) ---

def get_issue_details_cache(locg_issue_id, path=DB_PATH, max_age_days=None):
    """Cached details, or None. The key is a LOCG issue id for LOCG entries and
    'metron:<id>' for Metron ones (same table — the Details tab doesn't care).
    max_age_days: treat older entries as missing (variants keep arriving for
    an issue right up to release; LOCG entries were never re-fetched)."""
    import json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT data_json, fetched_at FROM issue_details_cache WHERE locg_issue_id = ?",
            (str(locg_issue_id),),
        ).fetchone()
        if not row:
            return None
        if max_age_days is not None and row["fetched_at"]:
            age = conn.execute("SELECT julianday('now') - julianday(?)", (row["fetched_at"],)).fetchone()[0]
            if age is not None and age > max_age_days:
                return None
        return json.loads(row["data_json"])


def set_issue_details_cache(locg_issue_id, data, path=DB_PATH):
    import json
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO issue_details_cache (locg_issue_id, data_json, fetched_at) "
            "VALUES (?, ?, datetime('now'))",
            (str(locg_issue_id), json.dumps(data)),
        )


# --- Series ---

def add_series(komga_series_id=None, title=None, publisher=None,
               year_began=None, folder_path=None, on_pull_list=True, locg_series_id=None,
               kind="series", cv_arc_id=None, cv_volume_id=None, path=DB_PATH) -> int:
    with _connect(path) as conn:
        cur = conn.execute("""
            INSERT INTO tracked_series (komga_series_id, title, publisher,
                                        year_began, folder_path, on_pull_list, locg_series_id,
                                        kind, cv_arc_id, cv_volume_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (komga_series_id, title, publisher, year_began,
              folder_path, int(on_pull_list), locg_series_id, kind, cv_arc_id, cv_volume_id))
        return cur.lastrowid


def set_series_cv_volume(series_id, cv_volume_id, path=DB_PATH):
    """Cache a series' resolved CV volume id (so arc discovery can scope to the right
    run — Batman 1940 vs 2025 — without re-resolving every time). When the anchor
    actually CHANGES (e.g. a Follow re-stamps a run off its old wrong volume), the
    arc-discovery cache is scoped to the old volume and now lies — drop it so the next
    Arcs-tab load re-discovers against the correct run."""
    with _connect(path) as conn:
        old = conn.execute("SELECT cv_volume_id FROM tracked_series WHERE id = ?",
                           (series_id,)).fetchone()
        conn.execute("UPDATE tracked_series SET cv_volume_id = ? WHERE id = ?",
                     (str(cv_volume_id), series_id))
        if not old or old[0] != str(cv_volume_id):
            conn.execute("DELETE FROM arc_discovery_cache WHERE tracked_series_id = ?",
                         (series_id,))


def get_series_by_cv_volume(cv_volume_id, path=DB_PATH):
    """A tracked series linked to this CV volume id, or None — the robust key for
    routing an arc's issues to the right run (Batman 1940, not 2016)."""
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM tracked_series WHERE cv_volume_id = ?",
                         (str(cv_volume_id),)).fetchone()
        return dict(r) if r else None


def get_series_by_komga_id(komga_series_id, path=DB_PATH):
    """A tracked series already linked to this Komga series, or None. komga_series_id
    is UNIQUE, so creating a second series with the same link IntegrityErrors — callers
    reuse the existing series instead of inserting a duplicate."""
    if not komga_series_id:
        return None
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM tracked_series WHERE komga_series_id = ?",
                         (str(komga_series_id),)).fetchone()
        return dict(r) if r else None


def remove_series(series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("DELETE FROM issue_status WHERE tracked_series_id = ?", (series_id,))
        conn.execute("DELETE FROM arc_issues WHERE arc_series_id = ?", (series_id,))
        conn.execute("DELETE FROM tracked_series WHERE id = ?", (series_id,))


def replace_arc_reading_order(arc_series_id, issues, path=DB_PATH):
    """Wipe + insert an arc's cross-title reading order. `issues` = list of dicts
    {reading_order, source_title, number, story_title, cv_issue_id, cv_volume_id}.
    Preserves any komga_book_id/owned already resolved (matched on reading_order)."""
    with _connect(path) as conn:
        prior = {r["reading_order"]: dict(r) for r in conn.execute(
            "SELECT reading_order, komga_book_id, owned FROM arc_issues WHERE arc_series_id = ?",
            (arc_series_id,))}
        conn.execute("DELETE FROM arc_issues WHERE arc_series_id = ?", (arc_series_id,))
        for it in issues:
            ro = it["reading_order"]
            keep = prior.get(ro, {})
            conn.execute("""
                INSERT INTO arc_issues (arc_series_id, reading_order, source_title, number,
                                        story_title, cv_issue_id, cv_volume_id, image_url,
                                        komga_book_id, owned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (arc_series_id, ro, it.get("source_title"), it.get("number"),
                  it.get("story_title"), it.get("cv_issue_id"), it.get("cv_volume_id"),
                  it.get("image_url"),
                  keep.get("komga_book_id"), keep.get("owned", 0)))


def get_arc_reading_order(arc_series_id, path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM arc_issues WHERE arc_series_id = ? ORDER BY reading_order",
            (arc_series_id,))]


def set_arc_ownership(arc_series_id, resolved, path=DB_PATH):
    """Stamp cross-title ownership onto an arc's rows. resolved = list of
    (reading_order, komga_book_id | None, owned 0/1) — one per arc_issue."""
    with _connect(path) as conn:
        for ro, book_id, owned in resolved:
            conn.execute(
                "UPDATE arc_issues SET komga_book_id = ?, owned = ? "
                "WHERE arc_series_id = ? AND reading_order = ?",
                (book_id, int(owned), arc_series_id, ro))


def find_series_by_title(title, path=DB_PATH):
    """A tracked (non-arc) series whose title matches, year-tolerant. Used to route
    an arc's collected-edition trade to its MAIN series (the lens model)."""
    from kometa.arc import titles_match
    with _connect(path) as conn:
        for r in conn.execute("SELECT * FROM tracked_series WHERE kind != 'arc'"):
            if titles_match(r["title"], title):
                return dict(r)
    return None


def find_arc_by_cv_id(cv_arc_id, path=DB_PATH):
    """An already-tracked arc with this ComicVine id, or None — so re-adding an arc
    opens the existing one instead of duplicating it."""
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM tracked_series WHERE kind = 'arc' AND cv_arc_id = ?",
                         (str(cv_arc_id),)).fetchone()
        return dict(r) if r else None


def get_all_arcs(path=DB_PATH):
    """Every story arc with its participating source titles + owned counts — the
    raw data behind a series' Arcs tab (caller filters by series title)."""
    with _connect(path) as conn:
        arcs = {r["id"]: {"id": r["id"], "title": r["title"], "cv_arc_id": r["cv_arc_id"],
                          "source_titles": set(), "cv_volume_ids": set(),
                          "issue_count": 0, "owned_count": 0}
                for r in conn.execute("SELECT id, title, cv_arc_id FROM tracked_series WHERE kind = 'arc'")}
        for r in conn.execute("SELECT arc_series_id, source_title, cv_volume_id, owned FROM arc_issues"):
            a = arcs.get(r["arc_series_id"])
            if a:
                a["source_titles"].add(r["source_title"])
                if r["cv_volume_id"]:
                    a["cv_volume_ids"].add(str(r["cv_volume_id"]))
                a["issue_count"] += 1
                a["owned_count"] += (r["owned"] or 0)
    for a in arcs.values():
        a["source_titles"] = sorted(a["source_titles"])
        a["cv_volume_ids"] = sorted(a["cv_volume_ids"])
    return list(arcs.values())


def get_all_series(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tracked_series ORDER BY title")]


# How far back a release still counts as THIS week's for the library's calendar
# sort. Comics drop weekly on Wednesdays — 6 days holds exactly the latest
# new-comic day and lets it go the morning the next one arrives.
RECENT_RELEASE_DAYS = 6


def calendar_date(recent_release, next_release):
    """The date a series sorts by on the library calendar: this week's release if
    it had one — owned or not — else its next upcoming one. next_release alone
    (the old key) skips owned issues, so the instant a new #1 downloaded its
    series fell out of today's slot to the bottom of the grid (Midnight
    Spider-Man, 2026-10-07): the books you most wanted to see, buried."""
    return recent_release or next_release


def get_all_series_summaries(path=DB_PATH):
    """Bulk per-series aggregation: counts, next-release date, and the card cover.

    card_image is the library card's cover, sourced exactly like the issue tile so
    the two never disagree. The card issue is the soonest upcoming release (within
    30d) or, if none, the most recently RELEASED issue. Its cover resolves:
      1. your picked variant (variant_prefs), then
      2. the Komga book thumbnail — the real cover of the file you own (a variant
         edition shows here even if you never used the picker), then
      3. the Metron solicit art.
    Upcoming issues aren't owned, so they have no Komga book — variant → Metron.
    The up_/recent_ columns are scratch and aren't returned."""
    today = str(date.today())
    cutoff = str(date.today() + timedelta(days=30))
    week_ago = str(date.today() - timedelta(days=RECENT_RELEASE_DAYS))
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT
                tracked_series_id,
                MAX(CASE WHEN ignored = 0 AND store_date IS NOT NULL AND store_date >= ? AND store_date <= ? THEN store_date END) as recent_release,
                SUM(CASE WHEN owned = 1 THEN 1 ELSE 0 END) as owned,
                SUM(CASE WHEN owned = 0 AND ignored = 0 AND (store_date IS NULL OR store_date < ?) THEN 1 ELSE 0 END) as missing,
                SUM(CASE WHEN owned = 0 AND ignored = 0 AND store_date IS NOT NULL AND store_date >= ? THEN 1 ELSE 0 END) as upcoming,
                SUM(CASE WHEN owned = 0 AND ignored = 0 AND store_date = ? THEN 1 ELSE 0 END) as out_today,
                MIN(CASE WHEN owned = 0 AND ignored = 0 AND store_date IS NOT NULL AND store_date >= ? AND store_date <= ? THEN store_date END) as next_release,
                (SELECT number FROM issue_status i2 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.owned = 0 AND i2.store_date >= ? AND i2.store_date <= ? AND i2.metron_image IS NOT NULL
                   ORDER BY i2.store_date ASC LIMIT 1) as up_number,
                (SELECT metron_image FROM issue_status i2 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.owned = 0 AND i2.store_date >= ? AND i2.store_date <= ? AND i2.metron_image IS NOT NULL
                   ORDER BY i2.store_date ASC LIMIT 1) as up_image,
                (SELECT number FROM issue_status i2 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.store_date IS NOT NULL AND i2.store_date <= ?
                   ORDER BY i2.store_date DESC LIMIT 1) as recent_number,
                (SELECT metron_image FROM issue_status i2 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.store_date IS NOT NULL AND i2.store_date <= ?
                   ORDER BY i2.store_date DESC LIMIT 1) as recent_image,
                (SELECT komga_book_id FROM issue_status i2 WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.store_date IS NOT NULL AND i2.store_date <= ?
                   ORDER BY i2.store_date DESC LIMIT 1) as recent_komga,
                (SELECT kv.v FROM issue_status i2 JOIN komga_book_version kv ON kv.book_id = i2.komga_book_id
                   WHERE i2.tracked_series_id = issue_status.tracked_series_id
                   AND i2.store_date IS NOT NULL AND i2.store_date <= ?
                   ORDER BY i2.store_date DESC LIMIT 1) as recent_komga_v
            FROM issue_status
            GROUP BY tracked_series_id
        """, (week_ago, today, today, today, today, today, cutoff, today, cutoff, today, cutoff, today, today, today, today))
        rows = [dict(r) for r in rows]

        # Resolve every variant pick (owned + upcoming) to a cover URL — same logic
        # get_issues_for_series uses — so the card can prefer your chosen cover.
        variant_map = {}
        for vp in conn.execute("SELECT tracked_series_id, number, selected, primary_id FROM variant_prefs"):
            try:
                sel = json.loads(vp["selected"])
                prim = next((c for c in sel if c.get("id") == vp["primary_id"]), None)
                if prim:
                    variant_map[(vp["tracked_series_id"], vp["number"])] = prim.get("large") or prim.get("thumb")
            except Exception:
                pass

    out = {}
    for r in rows:
        sid = r["tracked_series_id"]
        if r["up_number"] is not None:
            # Upcoming (not owned): your variant for it, else its solicit art.
            card_image = variant_map.get((sid, r["up_number"])) or r["up_image"]
        else:
            # Most recent released: variant → the real file cover (Komga) → solicit.
            cn = r["recent_number"]
            card_image = variant_map.get((sid, cn)) if cn is not None else None
            if not card_image and r["recent_komga"]:
                card_image = book_thumb_url(r["recent_komga"], r["recent_komga_v"])
            if not card_image:
                card_image = r["recent_image"]
        out[sid] = {
            "owned": r["owned"], "missing": r["missing"], "upcoming": r["upcoming"],
            "next_release": r["next_release"], "card_image": card_image,
            "calendar_date": calendar_date(r["recent_release"], r["next_release"]),
            # Library card only: released today, not on the shelf yet. Still counted
            # as upcoming everywhere else — the card just shouldn't read 'complete'.
            "out_today": r["out_today"] or 0,
        }
    return out


def get_series_by_id(series_id, path=DB_PATH):
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        return dict(row) if row else None


def set_owned(tracked_series_id, number, owned, path=DB_PATH):
    """Flip just the owned flag on an existing issue — used by folder scanning,
    which is the source of truth for ownership (no metadata touched)."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE issue_status SET owned = ? WHERE tracked_series_id = ? AND number = ?",
            (int(owned), tracked_series_id, number),
        )


def set_owned_bulk(tracked_series_id, numbers, owned, path=DB_PATH):
    """set_owned for many issue numbers in ONE connection/transaction. The sync
    reconcile loops were opening a connection per flipped issue."""
    if not numbers:
        return
    with _connect(path) as conn:
        conn.executemany(
            "UPDATE issue_status SET owned = ? WHERE tracked_series_id = ? AND number = ?",
            [(int(owned), tracked_series_id, n) for n in numbers],
        )


_UPSERT_ISSUE_SQL = """
    INSERT INTO issue_status (tracked_series_id, number, store_date, owned, komga_book_id, metron_image, locg_issue_id, metron_issue_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(tracked_series_id, number) DO UPDATE SET
        store_date      = COALESCE(excluded.store_date, store_date),
        owned        = excluded.owned,
        komga_book_id   = excluded.komga_book_id,
        metron_image    = excluded.metron_image,
        locg_issue_id   = COALESCE(excluded.locg_issue_id, locg_issue_id),
        metron_issue_id = COALESCE(excluded.metron_issue_id, metron_issue_id)
"""


def upsert_issue_status(tracked_series_id, number, store_date, owned, komga_book_id=None, metron_image=None,
                        locg_issue_id=None, metron_issue_id=None, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(_UPSERT_ISSUE_SQL,
                     (tracked_series_id, number, store_date, int(owned), komga_book_id, metron_image,
                      locg_issue_id, metron_issue_id))


def upsert_issue_status_many(rows, path=DB_PATH):
    """Full-fidelity upsert_issue_status for many rows in ONE transaction. rows =
    (tracked_series_id, number, store_date, owned, komga_book_id, metron_image,
    locg_issue_id[, metron_issue_id]) tuples. The per-issue sync loop was paying
    a connect/commit (fsync) cycle per issue, per series, per sync."""
    if not rows:
        return
    with _connect(path) as conn:
        conn.executemany(_UPSERT_ISSUE_SQL,
                         [(r[0], r[1], r[2], int(r[3]), r[4], r[5], r[6], r[7] if len(r) > 7 else None)
                          for r in rows])


def set_komga_book_id(series_id, number, book_id, path=DB_PATH):
    """Stamp just the komga_book_id on an existing issue (no-op if the row doesn't
    exist). Used to apply the Komga book map to disk-derived owned issues without
    clobbering their other fields the way a full upsert would."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE issue_status SET komga_book_id = ? WHERE tracked_series_id = ? AND number = ?",
            (book_id, series_id, number),
        )


def set_komga_book_ids_bulk(series_id, book_map, path=DB_PATH):
    """set_komga_book_id for a whole {number: book_id} map in ONE transaction."""
    if not book_map:
        return
    with _connect(path) as conn:
        conn.executemany(
            "UPDATE issue_status SET komga_book_id = ? WHERE tracked_series_id = ? AND number = ?",
            [(bid, series_id, num) for num, bid in book_map.items()],
        )


def set_pull_list(series_id, on_pull_list, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET on_pull_list = ? WHERE id = ?",
            (int(on_pull_list), series_id),
        )


def set_page_max(series_id, page_max, path=DB_PATH):
    """Per-series single-issue page ceiling; None reverts to the global default."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET page_max = ? WHERE id = ?",
            (page_max, series_id),
        )


def mark_synced(series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            UPDATE tracked_series SET last_synced = datetime('now') WHERE id = ?
        """, (series_id,))


def set_trades(tracked_series_id, trades, path=DB_PATH):
    """Cache a series' collected-edition list (already variant-folded). Stamps
    fetched_at so the read side can decide when it's stale."""
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO trades_cache (tracked_series_id, data_json, fetched_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(tracked_series_id) DO UPDATE SET
                data_json = excluded.data_json,
                fetched_at = excluded.fetched_at
        """, (tracked_series_id, json.dumps(trades)))


def get_trades(tracked_series_id, path=DB_PATH):
    """Cached trades + age in seconds, or None if never fetched.
    Returns {'trades': [...], 'age': float}."""
    with _connect(path) as conn:
        row = conn.execute("""
            SELECT data_json, (julianday('now') - julianday(fetched_at)) * 86400 AS age
            FROM trades_cache WHERE tracked_series_id = ?
        """, (tracked_series_id,)).fetchone()
    if not row:
        return None
    return {"trades": json.loads(row["data_json"]), "age": row["age"]}


def set_arc_discovery(tracked_series_id, arcs, path=DB_PATH):
    """Cache a series' Wikipedia-discovered arc list (arcs barely change, so this
    stays fresh for days)."""
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO arc_discovery_cache (tracked_series_id, data_json, fetched_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(tracked_series_id) DO UPDATE SET
                data_json = excluded.data_json, fetched_at = excluded.fetched_at
        """, (tracked_series_id, json.dumps(arcs)))


def get_arc_discovery(tracked_series_id, path=DB_PATH):
    """Cached discovered arcs + age in seconds, or None. {'arcs': [...], 'age': float}."""
    with _connect(path) as conn:
        row = conn.execute("""
            SELECT data_json, (julianday('now') - julianday(fetched_at)) * 86400 AS age
            FROM arc_discovery_cache WHERE tracked_series_id = ?
        """, (tracked_series_id,)).fetchone()
    if not row:
        return None
    return {"arcs": json.loads(row["data_json"]), "age": row["age"]}


def book_thumb_url(book_id: str, v: str | None = None) -> str:
    """The one spelling of a Komga cover URL — versioned when we know the file."""
    from urllib.parse import quote
    return f"/api/book/{book_id}/thumbnail" + (f"?v={quote(v)}" if v else "")


def set_komga_book_versions(versions: dict, path=DB_PATH):
    """{book_id: v} from a Komga book list, in one transaction."""
    if not versions:
        return
    with _connect(path) as conn:
        conn.executemany(
            "INSERT INTO komga_book_version (book_id, v) VALUES (?, ?) "
            "ON CONFLICT(book_id) DO UPDATE SET v = excluded.v",
            list(versions.items()))


def get_issues_for_series(tracked_series_id, path=DB_PATH):
    with _connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT i.*, kv.v AS komga_book_v FROM issue_status i
            LEFT JOIN komga_book_version kv ON kv.book_id = i.komga_book_id
            WHERE i.tracked_series_id = ? ORDER BY i.number
        """, (tracked_series_id,))]
        prefs = {r["number"]: r for r in conn.execute(
            "SELECT number, selected, primary_id FROM variant_prefs WHERE tracked_series_id = ?",
            (tracked_series_id,)
        )}
    # Stamp variant_cover = the chosen variant's image on issues that have a saved pref.
    # Lets the modal header show YOUR pick for an upcoming/not-yet-downloaded issue
    # (owned issues get their cover from the injected CBZ via Komga, no pref kept).
    for row in rows:
        p = prefs.get(row["number"])
        if not p:
            continue
        try:
            sel = json.loads(p["selected"])
            prim = next((c for c in sel if c.get("id") == p["primary_id"]), None)
            if prim:
                row["variant_cover"] = prim.get("large") or prim.get("thumb")
        except (ValueError, TypeError):
            pass
    return rows


def rename_book_path(old_path, new_path, path=DB_PATH) -> int:
    """A file moved: the book row follows it, read progress rides along (it's keyed
    on the book id, not the path). The number is re-read from the NEW name —
    'Dr. Manhattan TPB 01.cbz' had none, 'Before Watchmen - Dr. Manhattan #001'
    does, and a book that keeps a stale null shows twice on the series page
    (as issue #1 and again under 'Also on the shelf'). kometa/tidy.py."""
    import os
    from kometa.naming import parse_issue_number
    with _connect(path) as conn:
        n = conn.execute("UPDATE books SET path = ? WHERE path = ?", (new_path, old_path)).rowcount
        if n:
            row = conn.execute("""
                SELECT b.id, b.number, COALESCE(t.title, s.title, '') AS title FROM books b
                LEFT JOIN tracked_series t ON t.id = b.tracked_series_id
                LEFT JOIN shelf_series s ON s.id = b.shelf_series_id WHERE b.path = ?""", (new_path,)).fetchone()
            if row:
                num = parse_issue_number(os.path.basename(new_path), row["title"])
                if num is not None and num != row["number"]:
                    conn.execute("UPDATE books SET number = ? WHERE id = ?", (num, row["id"]))
        return n


def reparse_null_numbers(path=DB_PATH) -> int:
    """Books indexed with no number whose name parses to one now: give them it.
    The heal for everything renamed before rename_book_path learned to."""
    import os
    from kometa.naming import parse_issue_number
    fixed = 0
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT b.id, b.path, COALESCE(t.title, s.title, '') AS title FROM books b
            LEFT JOIN tracked_series t ON t.id = b.tracked_series_id
            LEFT JOIN shelf_series s ON s.id = b.shelf_series_id WHERE b.number IS NULL""").fetchall()
        for r in rows:
            num = parse_issue_number(os.path.basename(r["path"]), r["title"])
            if num is not None:
                conn.execute("UPDATE books SET number = ? WHERE id = ?", (num, r["id"]))
                fixed += 1
    return fixed


def move_folder_paths(series_id, old_folder, new_folder, path=DB_PATH) -> int:
    """A series folder moved: every path that pointed inside it follows — book rows,
    the shelf row, the series itself. One transaction, so a crash can't leave
    half the library pointing at a folder that no longer exists."""
    old_prefix = old_folder.rstrip("/") + "/"
    new_prefix = new_folder.rstrip("/") + "/"
    with _connect(path) as conn:
        n = conn.execute(
            "UPDATE books SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
            (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix)).rowcount
        conn.execute("UPDATE shelf_series SET folder_path = ? WHERE folder_path = ?", (new_folder, old_folder))
        conn.execute("UPDATE tracked_series SET folder_path = ? WHERE id = ?", (new_folder, series_id))
        return n


def move_books_to_series(old_folder, new_folder, parent_id, parent_shelf_id, path=DB_PATH) -> int:
    """Files filed under another series (a trade into its run): book rows follow
    the move AND change owner, so read progress lands on the right series page."""
    old_prefix = old_folder.rstrip("/") + "/"
    new_prefix = new_folder.rstrip("/") + "/"
    with _connect(path) as conn:
        return conn.execute(
            "UPDATE books SET path = ? || substr(path, ?), tracked_series_id = ?, shelf_series_id = ? "
            "WHERE substr(path, 1, ?) = ?",
            (new_prefix, len(old_prefix) + 1, parent_id, parent_shelf_id, len(old_prefix), old_prefix)).rowcount


def set_book_owner(book_path, tracked_series_id, shelf_series_id, number="keep", path=DB_PATH) -> int:
    """number: the issue number to store — None for 'this is not an issue' (a
    filed collection). The shelf upsert COALESCEs numbers, so a stale '1' from
    an old '#001' name survives rescans unless cleared here explicitly."""
    with _connect(path) as conn:
        if number == "keep":
            return conn.execute("UPDATE books SET tracked_series_id = ?, shelf_series_id = ? WHERE path = ?",
                                (tracked_series_id, shelf_series_id, book_path)).rowcount
        return conn.execute("UPDATE books SET tracked_series_id = ?, shelf_series_id = ?, number = ? WHERE path = ?",
                            (tracked_series_id, shelf_series_id, number, book_path)).rowcount


def book_names_by_series(path=DB_PATH) -> dict[int, list[str]]:
    """{tracked_series_id: [file name, …]} from the books table — one query for
    the whole shelf, instead of a directory listing per series over SMB (the
    clean-up reports took 47 s each that way)."""
    import os
    out: dict[int, list[str]] = {}
    with _connect(path) as conn:
        for r in conn.execute("SELECT tracked_series_id, path FROM books WHERE tracked_series_id IS NOT NULL ORDER BY path"):
            out.setdefault(r["tracked_series_id"], []).append(os.path.basename(r["path"]))
    return out


def set_series_title(series_id, title, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET title = ? WHERE id = ?", (title, series_id))


def set_folder_path(series_id, folder_path, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET folder_path = ? WHERE id = ?",
            (folder_path, series_id),
        )


def set_locg_fetched(series_id, when, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET locg_fetched_at = ? WHERE id = ?", (when, series_id))


def set_locg_series_id(series_id, locg_series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET locg_fetched_at = NULL WHERE id = ?", (series_id,))
        conn.execute(
            "UPDATE tracked_series SET locg_series_id = ? WHERE id = ?",
            (locg_series_id, series_id),
        )


def set_komga_series_id(series_id, komga_series_id, path=DB_PATH) -> bool:
    """Link a series to its Komga counterpart. komga_series_id is UNIQUE, so this
    returns False (no-op) if that Komga series is already linked to another series."""
    import sqlite3
    with _connect(path) as conn:
        try:
            conn.execute(
                "UPDATE tracked_series SET komga_series_id = ? WHERE id = ?",
                (str(komga_series_id), series_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False



def clear_komga_series_id(series_id, path=DB_PATH):
    """Unlink a series from Komga — its stored id points at nothing any more."""
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET komga_series_id = NULL WHERE id = ?",
                     (series_id,))

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


def queue_issues_bulk(pairs, path=DB_PATH):
    """Queue many (tracked_series_id, issue_number) in ONE transaction. Per-call
    queue_issue commits (and fsyncs) once each — N issues = N fsyncs, brutal on NAS
    disk. Fulfilling an arc enqueues a whole reading order, so batch it."""
    with _connect(path) as conn:
        for sid, num in pairs:
            conn.execute("""
                INSERT INTO download_queue (tracked_series_id, issue_number, state)
                VALUES (?, ?, 'queued')
                ON CONFLICT(tracked_series_id, issue_number) DO UPDATE SET
                    state      = CASE WHEN state IN ('failed', 'not_found') THEN 'queued' ELSE state END,
                    error      = CASE WHEN state IN ('failed', 'not_found') THEN NULL ELSE error END,
                    updated_at = datetime('now')
            """, (sid, num))
    return len(pairs)


def upsert_issue_status_bulk(rows, path=DB_PATH):
    """Upsert many issues in ONE transaction (same fsync reason as above). rows =
    list of (tracked_series_id, number, owned, komga_book_id[, image]). The optional
    5th element is a cover URL stored in metron_image (COALESCE'd — a real cover is
    never nulled by a later row that lacks one). Leaves other metadata untouched."""
    with _connect(path) as conn:
        for row in rows:
            sid, num, owned, kbid = row[0], row[1], row[2], row[3]
            image = row[4] if len(row) > 4 else None
            conn.execute("""
                INSERT INTO issue_status (tracked_series_id, number, owned, komga_book_id, metron_image)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                    owned         = excluded.owned,
                    komga_book_id = excluded.komga_book_id,
                    metron_image  = COALESCE(excluded.metron_image, metron_image)
            """, (sid, num, int(owned), kbid, image))
    return len(rows)


def queue_trade(tracked_series_id, locg_id, title, vol=None, vol_range=None, cover=None,
                edition_title=None, pack_url=None, path=DB_PATH):
    """Queue a collected edition — same table, kind='trade'. meta_json carries the
    series title (for search), the edition's own title (for naming no-volume editions
    so they don't all collapse to one filename), vol info, and the cover for Activity.
    Re-queues a failed/not_found trade."""
    meta = json.dumps({"title": title, "vol": vol, "vol_range": vol_range,
                       "cover": cover, "edition_title": edition_title,
                       "pack_url": pack_url})
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO download_queue (tracked_series_id, kind, locg_id, meta_json, state)
            VALUES (?, 'trade', ?, ?, 'queued')
            ON CONFLICT(tracked_series_id, locg_id) DO UPDATE SET
                state      = CASE WHEN state IN ('failed', 'not_found') THEN 'queued' ELSE state END,
                error      = CASE WHEN state IN ('failed', 'not_found') THEN NULL ELSE error END,
                meta_json  = excluded.meta_json,
                updated_at = datetime('now')
        """, (tracked_series_id, locg_id, meta))


def complete_trade(qid, path=DB_PATH):
    """Mark a trade download done. Unlike complete_download there's no issue_status
    to reconcile — ownership is read from the folder (the file we just placed)."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET state='done', error=NULL, updated_at=datetime('now') WHERE id=?",
            (qid,))


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
            SELECT q.*, s.title, s.publisher, s.komga_series_id, s.year_began
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            ORDER BY q.updated_at DESC
        """)]


def reset_stuck_queue_items(path=DB_PATH):
    """Reset searching/downloading/processing items left orphaned by a container
    restart. 'processing' counts: a crash mid-finalize would otherwise strand the
    row in a state nothing ever advances."""
    with _connect(path) as conn:
        conn.execute("""
            UPDATE download_queue
            SET state = 'queued', error = NULL, sab_nzo_id = NULL, updated_at = datetime('now')
            WHERE state IN ('searching', 'downloading', 'processing')
        """)


def get_queued_items(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.komga_series_id, s.year_began, s.folder_path, s.page_max
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
            SELECT q.*, s.title, s.publisher, s.year_began, s.folder_path, s.page_max
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state = 'pending_usenet' AND q.sab_nzo_id IS NOT NULL
        """)]


def add_failed_source(queue_id, url, path=DB_PATH):
    """Record a release URL that failed DELIVERY for this queue row, so the next
    search attempt excludes it. Deduped, capped at 20 — a row that burns twenty
    releases has bigger problems than list growth."""
    if not url:
        return
    import json as _json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT failed_sources FROM download_queue WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            return
        try:
            sources = _json.loads(row[0]) if row[0] else []
        except (ValueError, TypeError):
            sources = []
        if url in sources:
            return
        sources = (sources + [url])[-20:]
        conn.execute(
            "UPDATE download_queue SET failed_sources = ?, updated_at = datetime('now') WHERE id = ?",
            (_json.dumps(sources), queue_id),
        )
        # ...and against the issue itself, so a fresh row for the same issue
        # inherits the verdict instead of re-buying the corpse.
        conn.execute(
            "INSERT OR IGNORE INTO failed_releases (tracked_series_id, issue_number, url) "
            "SELECT tracked_series_id, issue_number, ? FROM download_queue WHERE id = ?",
            (url, queue_id),
        )


def failed_releases_for(tracked_series_id, issue_number, path=DB_PATH) -> set:
    """Release URLs that already failed delivery for this issue, whatever row
    asked for it."""
    with _connect(path) as conn:
        return {r[0] for r in conn.execute(
            "SELECT url FROM failed_releases WHERE tracked_series_id = ? AND issue_number = ?",
            (tracked_series_id, issue_number))}


def add_failed_channel(queue_id, channel, path=DB_PATH):
    """Bench a delivery channel ('usenet'/'torrent') for this queue row — the
    next attempt skips it and starts at the next rung of the cascade."""
    import json as _json
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT failed_channels FROM download_queue WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            return
        try:
            channels = _json.loads(row[0]) if row[0] else []
        except (ValueError, TypeError):
            channels = []
        if channel in channels:
            return
        conn.execute(
            "UPDATE download_queue SET failed_channels = ?, updated_at = datetime('now') WHERE id = ?",
            (_json.dumps(channels + [channel]), queue_id),
        )


def set_sab_nzo_id(queue_id, nzo_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET sab_nzo_id = ?, updated_at = datetime('now') WHERE id = ?",
            (nzo_id, queue_id),
        )


def get_pending_torrent_items(path=DB_PATH):
    """Items waiting on qBittorrent to finish downloading — twin of the usenet one."""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.year_began, s.folder_path, s.page_max
            FROM download_queue q
            JOIN tracked_series s ON s.id = q.tracked_series_id
            WHERE q.state = 'pending_torrent' AND q.torrent_hash IS NOT NULL
        """)]


def set_torrent_hash(queue_id, torrent_hash, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET torrent_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (torrent_hash, queue_id),
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


def requeue_not_found(path=DB_PATH) -> int:
    """Re-arm every not_found job: fresh search, fresh rate-limit budget.
    Returns how many were re-queued. failed jobs are NOT touched — those
    usually need a human, and they have their own Retry button."""
    with _connect(path) as conn:
        cur = conn.execute("""
            UPDATE download_queue
            SET state = 'queued', error = NULL, retry_after = NULL,
                rl_attempts = 0, updated_at = datetime('now')
            WHERE state = 'not_found'
        """)
        return cur.rowcount


def reset_rl_attempts(queue_id, path=DB_PATH):
    """Manual retry = human says go — give the job a fresh rate-limit budget."""
    with _connect(path) as conn:
        conn.execute("UPDATE download_queue SET rl_attempts = 0 WHERE id = ?", (queue_id,))


def bump_rl_attempts(queue_id, path=DB_PATH) -> int:
    """Increment and return this job's consecutive rate-limit counter."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET rl_attempts = COALESCE(rl_attempts, 0) + 1 WHERE id = ?",
            (queue_id,),
        )
        row = conn.execute("SELECT rl_attempts FROM download_queue WHERE id = ?", (queue_id,)).fetchone()
        return row[0] if row else 0


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
                                      komga_book_id, metron_image, locg_issue_id)
            VALUES (?, ?, ?, 1, NULL, NULL, NULL)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                store_date      = COALESCE(excluded.store_date, store_date),
                owned        = excluded.owned,
                komga_book_id   = excluded.komga_book_id,
                metron_image    = excluded.metron_image,
                locg_issue_id   = COALESCE(excluded.locg_issue_id, locg_issue_id)
        """, (tracked_series_id, issue_number, store_date))


def dequeue_waiting_issue(series_id, number, path=DB_PATH) -> int:
    """Drop an issue's queue rows that are only WAITING (queued, or parked as
    not_found/failed for a retry). Anything mid-search or mid-download is left to
    finish — yanking a row out from under a worker is how state goes inconsistent."""
    with _connect(path) as conn:
        return conn.execute(
            "DELETE FROM download_queue WHERE tracked_series_id = ? AND issue_number = ? "
            "AND state IN ('queued', 'not_found', 'failed')",
            (series_id, number),
        ).rowcount


def dequeue_waiting_series(series_id, path=DB_PATH) -> int:
    """Pull list switched off: drop the series' queue rows that are only WAITING.
    In-flight searches/downloads finish (same rule as dequeue_waiting_issue)."""
    with _connect(path) as conn:
        return conn.execute(
            "DELETE FROM download_queue WHERE tracked_series_id = ? "
            "AND state IN ('queued', 'not_found', 'failed')", (series_id,)).rowcount


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
              AND i.ignored = 0
              AND (i.store_date IS NULL OR i.store_date <= date('now'))
              AND s.monitor_status = 'monitored'
              AND s.on_pull_list = 1
            GROUP BY s.id
        """)
        return {r["id"]: r["cnt"] for r in rows}


def pack_attempt_failed(series_id, path=DB_PATH) -> bool:
    """True if a usenet pack for this series has already been tried and couldn't
    deliver. Its own shot is spent — the caller hands off to the next source
    instead of submitting the same doomed pack on every sweep forever."""
    with _connect(path) as conn:
        row = conn.execute("""
            SELECT 1 FROM download_queue
            WHERE tracked_series_id = ? AND issue_number = -1
              AND state IN ('failed', 'not_found')
            LIMIT 1
        """, (series_id,)).fetchone()
    return row is not None


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
              AND i.ignored = 0
              AND (i.store_date IS NULL OR i.store_date <= date('now'))
              AND s.on_pull_list = 1
              AND NOT EXISTS (
                  SELECT 1 FROM download_queue q
                  WHERE q.tracked_series_id = s.id
                    AND q.issue_number = i.number
                    AND q.state NOT IN ('failed', 'not_found')
              )
        """)]


def set_issue_ignored(series_id, number, ignored: bool, path=DB_PATH) -> bool:
    """Flag/unflag one issue as not-wanted. Returns False if the issue doesn't exist."""
    with _connect(path) as conn:
        return conn.execute(
            "UPDATE issue_status SET ignored = ? WHERE tracked_series_id = ? AND number = ?",
            (int(bool(ignored)), series_id, number),
        ).rowcount > 0


def get_upcoming_issues(days=90, past=0, path=DB_PATH):
    # `lookback` is built ONLY from int(past) or a literal — no string ever
    # reaches it, so the f-string can't inject. `days` is a bound param.
    if past:
        lookback = f"date('now', '-{int(past)} days')"
    else:
        lookback = "date('now', '-7 days', 'weekday 0')"
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute(f"""
            SELECT s.id, s.title, i.number, i.store_date, i.owned, i.ignored
            FROM issue_status i
            JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.store_date IS NOT NULL
              AND i.store_date >= {lookback}
              AND i.store_date <= date('now', ? || ' days')
              AND s.on_pull_list = 1
            ORDER BY i.store_date, s.title
        """, (str(days),))]  # nosec B608 — lookback is int/literal-only, days is bound


def set_variant_prefs(tracked_series_id, number, selected: list, primary_id: str, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO variant_prefs (tracked_series_id, number, selected, primary_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                selected   = excluded.selected,
                primary_id = excluded.primary_id
        """, (tracked_series_id, number, json.dumps(selected), primary_id))


def get_variant_prefs(tracked_series_id, number, path=DB_PATH):
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


# --- reader: books + progress -------------------------------------------------

def upsert_book(path_, size, mtime, pages, tracked_series_id=None, number=None, path=DB_PATH) -> int:
    """Register (or refresh) a comic file. pages = [[name, w, h], ...]. Returns id."""
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO books (path, size, mtime, page_count, pages_json, tracked_series_id, number)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size, mtime = excluded.mtime,
                page_count = excluded.page_count, pages_json = excluded.pages_json,
                tracked_series_id = COALESCE(excluded.tracked_series_id, tracked_series_id),
                number = COALESCE(excluded.number, number)
        """, (path_, size, mtime, len(pages), json.dumps(pages), tracked_series_id, number))
        return conn.execute("SELECT id FROM books WHERE path = ?", (path_,)).fetchone()["id"]


def _book_row(r):
    if not r:
        return None
    b = dict(r)
    b["pages"] = json.loads(b.pop("pages_json") or "[]")
    return b


def get_book(book_id, path=DB_PATH):
    with _connect(path) as conn:
        return _book_row(conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone())


def get_book_by_path(path_, path=DB_PATH):
    with _connect(path) as conn:
        return _book_row(conn.execute("SELECT * FROM books WHERE path = ?", (path_,)).fetchone())


def get_progress(reader_id, book_id, path=DB_PATH):
    with _connect(path) as conn:
        r = conn.execute("SELECT page, completed, updated_at FROM read_progress "
                         "WHERE reader_id = ? AND book_id = ?", (reader_id, book_id)).fetchone()
        return dict(r) if r else None


def set_progress(reader_id, book_id, page, completed, updated_at, path=DB_PATH):
    """Write progress unless it's stale. Returns (accepted, stored_row).

    Stale = older than what's already stored. Equal timestamps are accepted
    (same device re-sending is harmless). The compare is on ISO-8601 strings, so
    clients must send UTC 'YYYY-MM-DDTHH:MM:SS(.fff)Z'."""
    with _connect(path) as conn:
        cur = conn.execute("SELECT page, completed, updated_at FROM read_progress "
                           "WHERE reader_id = ? AND book_id = ?", (reader_id, book_id)).fetchone()
        if cur and cur["updated_at"] > updated_at:
            return False, dict(cur)
        conn.execute("""
            INSERT INTO read_progress (reader_id, book_id, page, completed, updated_at, dismissed)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(reader_id, book_id) DO UPDATE SET
                page = excluded.page, completed = excluded.completed, updated_at = excluded.updated_at,
                dismissed = 0
        """, (reader_id, book_id, int(page), int(bool(completed)), updated_at))
        return True, {"page": int(page), "completed": int(bool(completed)), "updated_at": updated_at}


def set_dismissed(reader_id, book_id, dismissed: bool, path=DB_PATH) -> bool:
    """On Deck 'not now': hide from Continue reading without touching the place.
    Reading it again (any progress write) clears the flag by itself."""
    with _connect(path) as conn:
        return conn.execute("UPDATE read_progress SET dismissed = ? WHERE reader_id = ? AND book_id = ?",
                            (int(bool(dismissed)), reader_id, book_id)).rowcount > 0


# --- shelf index ------------------------------------------------------------------

def upsert_shelf_series(folder, title, publisher, tracked_series_id, book_count, scanned_at, path=DB_PATH) -> int:
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO shelf_series (folder_path, title, publisher, tracked_series_id, book_count, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(folder_path) DO UPDATE SET
                title = excluded.title, publisher = excluded.publisher,
                tracked_series_id = excluded.tracked_series_id,
                book_count = excluded.book_count, scanned_at = excluded.scanned_at
        """, (folder, title, publisher, tracked_series_id, book_count, scanned_at))
        return conn.execute("SELECT id FROM shelf_series WHERE folder_path = ?", (folder,)).fetchone()["id"]


def index_books(rows, path=DB_PATH):
    """rows = (path, size, mtime, number, shelf_series_id, tracked_series_id).
    Registers files WITHOUT opening them. A changed size/mtime clears the page
    list so the reader rescans the file on next open."""
    with _connect(path) as conn:
        conn.executemany("""
            INSERT INTO books (path, size, mtime, number, shelf_series_id, tracked_series_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                pages_json = CASE WHEN books.size IS excluded.size AND books.mtime IS excluded.mtime
                                  THEN books.pages_json ELSE NULL END,
                page_count = CASE WHEN books.size IS excluded.size AND books.mtime IS excluded.mtime
                                  THEN books.page_count ELSE NULL END,
                size = excluded.size, mtime = excluded.mtime,
                number = COALESCE(excluded.number, books.number),
                shelf_series_id = excluded.shelf_series_id,
                tracked_series_id = COALESCE(excluded.tracked_series_id, books.tracked_series_id)
        """, rows)


def remove_books_under(folder, path=DB_PATH) -> int:
    """Forget every book (and its read progress) filed under a folder — the
    folder has left the shelf (kometa/trash.py)."""
    prefix = folder.rstrip("/") + "/"
    with _connect(path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM books WHERE path LIKE ? ESCAPE '\\'",
                                          (prefix.replace("%", r"\%").replace("_", r"\_") + "%",))]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM read_progress WHERE book_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM books WHERE id IN ({marks})", ids)
        return len(ids)


def remove_shelf_series_by_path(folder, path=DB_PATH) -> int:
    with _connect(path) as conn:
        return conn.execute("DELETE FROM shelf_series WHERE folder_path = ?", (folder,)).rowcount


def prune_shelf(scanned_at, path=DB_PATH) -> int:
    """Drop series folders the latest scan didn't see. Book rows (and any read
    progress on them) are kept — a book that comes back keeps its history."""
    with _connect(path) as conn:
        return conn.execute("DELETE FROM shelf_series WHERE scanned_at IS NOT ? ",
                            (scanned_at,)).rowcount


_SHELF_STATS_SQL = """
    SELECT b.shelf_series_id AS sid,
           SUM(CASE WHEN p.completed = 1 THEN 1 ELSE 0 END) AS read_count,
           SUM(CASE WHEN p.completed = 0 THEN 1 ELSE 0 END) AS in_progress,
           MAX(p.updated_at) AS last_read
    FROM books b JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
    GROUP BY b.shelf_series_id
"""


def list_shelf(reader_id, untracked_only=True, path=DB_PATH):
    with _connect(path) as conn:
        stats = {r["sid"]: dict(r) for r in conn.execute(_SHELF_STATS_SQL, (reader_id,))}
        q = "SELECT * FROM shelf_series" + (" WHERE tracked_series_id IS NULL" if untracked_only else "")
        out = []
        for r in conn.execute(q + " ORDER BY title COLLATE NOCASE"):
            d = dict(r)
            st = stats.get(d["id"], {})
            d.update(read_count=st.get("read_count") or 0, in_progress=st.get("in_progress") or 0,
                     last_read=st.get("last_read"))
            out.append(d)
        return out


def get_shelf_series(shelf_id, path=DB_PATH):
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM shelf_series WHERE id = ?", (shelf_id,)).fetchone()
        return dict(r) if r else None


def shelf_books(shelf_id, reader_id, path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT b.id, b.path, b.number, b.page_count, b.size, b.mtime,
                   p.page AS progress_page, p.completed, p.updated_at
            FROM books b LEFT JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE b.shelf_series_id = ?
        """, (reader_id, shelf_id))]


def reading_by_tracked_series(reader_id, path=DB_PATH) -> dict[int, dict]:
    """{tracked_series_id: {read_count, in_progress, last_read}} — the Library's
    Reading chip for tracked series."""
    with _connect(path) as conn:
        return {r["tid"]: {"read_count": r["read_count"], "in_progress": r["in_progress"], "last_read": r["last_read"]}
                for r in conn.execute("""
            SELECT b.tracked_series_id AS tid,
                   SUM(CASE WHEN p.completed = 1 THEN 1 ELSE 0 END) AS read_count,
                   SUM(CASE WHEN p.completed = 0 THEN 1 ELSE 0 END) AS in_progress,
                   MAX(p.updated_at) AS last_read
            FROM books b JOIN read_progress p ON p.book_id = b.id AND p.reader_id = ?
            WHERE b.tracked_series_id IS NOT NULL
            GROUP BY b.tracked_series_id
        """, (reader_id,))}


def set_match_status(series_id, status, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET match_status = ? WHERE id = ?", (status, series_id))


def link_shelf_series(shelf_id, series_id, path=DB_PATH):
    """A shelf folder became a Kometa series: point the folder and its books at it."""
    with _connect(path) as conn:
        conn.execute("UPDATE shelf_series SET tracked_series_id = ? WHERE id = ?", (series_id, shelf_id))
        conn.execute("UPDATE books SET tracked_series_id = ? WHERE shelf_series_id = ?", (series_id, shelf_id))


def shelf_id_for_series(series_id, path=DB_PATH):
    with _connect(path) as conn:
        r = conn.execute("SELECT id FROM shelf_series WHERE tracked_series_id = ?", (series_id,)).fetchone()
        return r["id"] if r else None


def set_metron_series_id(series_id, metron_series_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = ?, metron_fetched_at = NULL WHERE id = ?",
                     (metron_series_id, series_id))


def set_metron_fetched(series_id, when, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET metron_fetched_at = ? WHERE id = ?", (when, series_id))


def set_metron_link(series_id, outcome, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET metron_link = ? WHERE id = ?", (outcome, series_id))


def set_metron_type(series_id, series_type, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET metron_type = ? WHERE id = ?", (series_type or "", series_id))


def set_from_list(series_id, list_id, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET from_list_id = ? WHERE id = ?", (list_id, series_id))


def set_locg_link(series_id, outcome, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_series SET locg_link = ? WHERE id = ?", (outcome, series_id))
