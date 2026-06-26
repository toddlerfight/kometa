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

        # arc_issues gained cv_volume_id (the authoritative CV volume per issue, for
        # routing each issue to the right tracked run — e.g. Batman 1940, not 2016).
        ai_cols = [r[1] for r in conn.execute("PRAGMA table_info(arc_issues)")]
        if "cv_volume_id" not in ai_cols:
            conn.execute("ALTER TABLE arc_issues ADD COLUMN cv_volume_id TEXT")

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


# --- Issue details cache (LOCG desc + credits for the Details tab; the external
#     kometa-recommend project also reads this cache) ---

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


# --- Series ---

def add_series(komga_series_id=None, metron_series_id=None, title=None, publisher=None,
               year_began=None, folder_path=None, on_pull_list=True, locg_series_id=None,
               kind="series", cv_arc_id=None, cv_volume_id=None, path=DB_PATH) -> int:
    with _connect(path) as conn:
        cur = conn.execute("""
            INSERT INTO tracked_series (komga_series_id, metron_series_id, title, publisher,
                                        year_began, folder_path, on_pull_list, locg_series_id,
                                        kind, cv_arc_id, cv_volume_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (komga_series_id, metron_series_id, title, publisher, year_began,
              folder_path, int(on_pull_list), locg_series_id, kind, cv_arc_id, cv_volume_id))
        return cur.lastrowid


def get_series_by_cv_volume(cv_volume_id, path=DB_PATH):
    """A tracked series linked to this CV volume id, or None — the robust key for
    routing an arc's issues to the right run (Batman 1940, not 2016)."""
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM tracked_series WHERE cv_volume_id = ?",
                         (str(cv_volume_id),)).fetchone()
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
                                        story_title, cv_issue_id, cv_volume_id, komga_book_id, owned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (arc_series_id, ro, it.get("source_title"), it.get("number"),
                  it.get("story_title"), it.get("cv_issue_id"), it.get("cv_volume_id"),
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
                          "source_titles": set(), "issue_count": 0, "owned_count": 0}
                for r in conn.execute("SELECT id, title, cv_arc_id FROM tracked_series WHERE kind = 'arc'")}
        for r in conn.execute("SELECT arc_series_id, source_title, owned FROM arc_issues"):
            a = arcs.get(r["arc_series_id"])
            if a:
                a["source_titles"].add(r["source_title"])
                a["issue_count"] += 1
                a["owned_count"] += (r["owned"] or 0)
    for a in arcs.values():
        a["source_titles"] = sorted(a["source_titles"])
    return list(arcs.values())


def get_all_series(path=DB_PATH):
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tracked_series ORDER BY title")]


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
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT
                tracked_series_id,
                SUM(CASE WHEN owned = 1 THEN 1 ELSE 0 END) as owned,
                SUM(CASE WHEN owned = 0 AND (store_date IS NULL OR store_date < ?) THEN 1 ELSE 0 END) as missing,
                SUM(CASE WHEN owned = 0 AND store_date IS NOT NULL AND store_date >= ? THEN 1 ELSE 0 END) as upcoming,
                MIN(CASE WHEN owned = 0 AND store_date IS NOT NULL AND store_date >= ? AND store_date <= ? THEN store_date END) as next_release,
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
                   ORDER BY i2.store_date DESC LIMIT 1) as recent_komga
            FROM issue_status
            GROUP BY tracked_series_id
        """, (today, today, today, cutoff, today, cutoff, today, cutoff, today, today, today))
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
                card_image = f"/api/book/{r['recent_komga']}/thumbnail"
            if not card_image:
                card_image = r["recent_image"]
        out[sid] = {
            "owned": r["owned"], "missing": r["missing"], "upcoming": r["upcoming"],
            "next_release": r["next_release"], "card_image": card_image,
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


def set_komga_book_id(series_id, number, book_id, path=DB_PATH):
    """Stamp just the komga_book_id on an existing issue (no-op if the row doesn't
    exist). Used to apply the Komga book map to disk-derived owned issues without
    clobbering their other fields the way a full upsert would."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE issue_status SET komga_book_id = ? WHERE tracked_series_id = ? AND number = ?",
            (book_id, series_id, number),
        )


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


def get_issues_for_series(tracked_series_id, path=DB_PATH):
    with _connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM issue_status WHERE tracked_series_id = ? ORDER BY number
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


def set_folder_path(series_id, folder_path, path=DB_PATH):
    with _connect(path) as conn:
        conn.execute(
            "UPDATE tracked_series SET folder_path = ? WHERE id = ?",
            (folder_path, series_id),
        )


def set_locg_series_id(series_id, locg_series_id, path=DB_PATH):
    with _connect(path) as conn:
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
    list of (tracked_series_id, number, owned, komga_book_id). Leaves metadata
    columns untouched on conflict."""
    with _connect(path) as conn:
        for sid, num, owned, kbid in rows:
            conn.execute("""
                INSERT INTO issue_status (tracked_series_id, number, owned, komga_book_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tracked_series_id, number) DO UPDATE SET
                    owned         = excluded.owned,
                    komga_book_id = excluded.komga_book_id
            """, (sid, num, int(owned), kbid))
    return len(rows)


def queue_trade(tracked_series_id, locg_id, title, vol=None, vol_range=None, cover=None,
                edition_title=None, path=DB_PATH):
    """Queue a collected edition — same table, kind='trade'. meta_json carries the
    series title (for search), the edition's own title (for naming no-volume editions
    so they don't all collapse to one filename), vol info, and the cover for Activity.
    Re-queues a failed/not_found trade."""
    meta = json.dumps({"title": title, "vol": vol, "vol_range": vol_range,
                       "cover": cover, "edition_title": edition_title})
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


def get_pending_torrent_items(path=DB_PATH):
    """Items waiting on qBittorrent to finish downloading — twin of the usenet one."""
    with _connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.*, s.title, s.publisher, s.year_began, s.folder_path
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
