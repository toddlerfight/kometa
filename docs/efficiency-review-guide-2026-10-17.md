# Codebase Efficiency & Performance Review (2026-10-17)

A verified, empirically measured guide on database optimization, test suite runtime, cache lifecycle, and recommendation hot paths for Kometa.

---

## 1. Executive Summary & Live Measured Baselines

All metrics measured against the live production database (Python 3.12, SQLite 3.45.1, 7,114 books, 840 series, 773,812 CBR community ratings across a 328 MB database) and container runtime:

| Area | Measured Baseline | Target | Root Cause / Verification | Action |
|---|---|---|---|---|
| **CBR Score Table Footprint** | **213 MB** (65% of entire DB) | **101.4 MB** | Composite PK autoindex (67 MB) + series index (53 MB) + table (101.4 MB) | Cache first-year in `cbr_people_series`, migrate to `WITHOUT ROWID`, drop series index (saves 112 MB) |
| **Fingerprint Scan** | **32 – 37 ms** (`COUNT(*), MAX(at)`) | **1.1 ms** (live) | Sequential scan of 773k rows in `_fingerprint()`; runs every 20s–5m | Track `scores_version` in `cbr_people_state` (avoids `MAX(rowid)` replace blind spot) |
| **Community Matrix Build** | **310 ms** (195 ms GROUP BY, warm total 0.85s) | **0.4 ms** (live memo) | Recalculated on issue record trickles; bulk of time is hub damping | Memoise `_community` in-memory keyed on `(cbr_stamp, linked)` (pinned by test) |
| **Test Suite Execution** | **348 s** (1,294 tests) | **38.4 s** (1,295 tests) | Reader clock leakage (279s) + live network / pace timeouts (30s) | Fixed via `_reset_reader_activity` fixture + stubbing un-injected pacing / rescue loops |
| **Cache Storage Bloat** | **33 MB** (13,252 rows) | **~28 MB** | 2,893 dead `metron:<id>` v1 rows (5.2 MB) unread since 2026-10-12 | One-time `DELETE FROM issue_details_cache WHERE locg_issue_id LIKE 'metron:%'` |
| **Activity Queue Polling** | **1 KB gzipped / 2 s** | **Optimal** | Already fixed on 2026-10-17 (`b0cde04`) via `/api/queue?since=` | Verified complete |

---

## 2. In-Depth Findings & Architecture

### Area A: `cbr_people_scores` Storage Footprint (213 MB $\to$ 101 MB)

The table `cbr_people_scores` and its indexes account for 65% of the database file:
- Main table alone: **101.4 MB**
- Composite primary key auto-index (`person, series_path, number`): **~67 MB**
- Secondary index (`idx_cbr_people_scores_series`): **~53 MB**

#### 1. Why `idx_cbr_people_scores_series` Exists and How to Remove It Safely
* **Live Usage:** The web app calls `/api/discover/community` and `/api/discover/community/because-ondeck` when rendering recommendation rows (`static/app.js:1215-1216`).
* **Measured Query:** Finding publication years for 12 candidate series paths in `recommend()` (`cbr_people.py:718`) takes **12.6 ms with the index** and **66.9 ms without**.
* **Clean Solution:** Store each run's `first_year` directly in `cbr_people_series` (a 2-column table of ~3k rows) during harvest, and read it from there. Once `cbr_people_series` serves the year, remove `CREATE INDEX IF NOT EXISTS idx_cbr_people_scores_series` from `kometa/cbr_people.py:87` and drop the index.

#### 2. Migrating to `WITHOUT ROWID`
* **Real Production Copy Measurement:**
  - Standard table + series index: **213 MB**
  - `WITHOUT ROWID` + series index: **157.4 MB** (secondary indexes in `WITHOUT ROWID` store the full composite primary key instead of a 64-bit integer rowid)
  - `WITHOUT ROWID` without series index: **101.4 MB** (**saves 112 MB**, reducing table footprint by more than half)
  - Query speed: `GROUP BY person, series_path` accelerates from **189 ms down to 129 ms**.
* **Migration Procedure ("The How"):**
  1. Remove `CREATE INDEX IF NOT EXISTS idx_cbr_people_scores_series` from `cbr_people.py:87` so the index is not recreated on the new table.
  2. Guarded one-time migration step inside `cbr_people.ensure_tables()`:
     ```sql
     -- Check sqlite_master for existing schema
     CREATE TABLE cbr_people_scores_v2 (
         person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL,
         score REAL NOT NULL, date TEXT, via TEXT, at TEXT,
         PRIMARY KEY (person, series_path, number)
     ) WITHOUT ROWID;
     INSERT INTO cbr_people_scores_v2 (person, series_path, number, score, date, via, at)
         SELECT person, series_path, number, score, date, via, at FROM cbr_people_scores;
     DROP TABLE cbr_people_scores;
     ALTER TABLE cbr_people_scores_v2 RENAME TO cbr_people_scores;
     ```
     This copy takes ~1.0 s in an atomic transaction.
  3. Invalidate cache via `scores_version` in `cbr_people_state`:
     `MAX(rowid)` has a minor blind spot without `AUTOINCREMENT`: an `INSERT OR REPLACE` on the row holding the highest `rowid` reuses that rowid. Bumping `scores_version` once per page or profile commit in `cbr_people.py` is 100% reliable and works seamlessly with `WITHOUT ROWID`.
  4. Physical disk reclamation:
     `deploy.sh` does not run `VACUUM`. The container has no `sqlite3` CLI, so execute `VACUUM` through Python at a quiet moment (outside the nightly backup window):
     ```bash
     docker exec kometa python3 -c "import sqlite3; c = sqlite3.connect('/data/kometa.db', timeout=60); c.execute('VACUUM'); c.close()"
     ```
     This shrinks the production database from **328 MB $\to$ ~211 MB** (328 - 112 MB for scores - 5 MB for dead cache).


---

### Area B: Test Suite Runtime (348s $\to$ 38.4s)

The test suite runtime was reduced from **5m 48s** to **38.4s** (all 1,295 tests passing, zero external network traffic):

1. **Reader Clock Leakage (Cut 279s):**
   * Added `_reset_reader_activity` in `tests/conftest.py` to reset `activity._last_page["at"] = 0.0` and clear `_book_pages` before/after each test.
2. **Third-Party Network Call in Webtoon Guard (Cut 15.4s):**
   * `test_webtoon_parks...` in `tests/test_acquisition.py` was opening real HTTPS connections to `getcomics.org:443` through `_rescue_later` executing inline. Stubbed `_gc_rescue` in that test.
3. **Pacing Sleep in LOCG Sweep (Cut 9.3s):**
   * `test_three_failures_in_a_row...` in `tests/test_locg_sweep.py` sat in `locg_client._pace` (3s `PACE_GAP_S` delays). Monkeypatched `_pace` in `_community` fixture.
4. **Unstubbed DNS Lookup:**
   * `test_issue_art_still_wins_over_trades` in `tests/test_series_thumbnail.py` attempted DNS lookups for host `"x"`. Stubbed `images.serve` in `wired` fixture.

---

### Area C: Recommendation Index & Community Memoisation

* **Measured Live Baseline:** Repeat `_community` calls answer in **0.4 ms**; `_fingerprint()` dropped from 32 ms to **1.1 ms**.
* **Pinned Behavior:** Pinned by `test_community_memo_skips_rebuild_on_unrelated_changes_and_recomputes_when_scores_land` in `tests/test_related_phase2.py`:
  - `_community` skips recomputation when only `issue_record` changes.
  - `_community` recomputes when a new score lands in `cbr_people_scores`.

---

### Area D: `issue_details_cache` Dead Key Pruning

* **Targeted Prune:**
  ```sql
  -- Verify count first (expected: 2,893):
  SELECT COUNT(*) FROM issue_details_cache WHERE locg_issue_id LIKE 'metron:%';
  -- Delete legacy v1 keys:
  DELETE FROM issue_details_cache WHERE locg_issue_id LIKE 'metron:%';
  ```
  Preserves active `metron2:` entries necessary for upstream Metron outage fallback.

---

## 3. Verified Verification Procedures

### Step 1: Query Plan & State Inspection
```bash
docker exec kometa python3 -c '
import sqlite3
con = sqlite3.connect("/data/kometa.db")

queries = [
    ("cbr_grouped_pk", "EXPLAIN QUERY PLAN SELECT person, series_path, AVG(score) FROM cbr_people_scores GROUP BY person, series_path"),
    ("cbr_max_rowid", "EXPLAIN QUERY PLAN SELECT MAX(rowid) FROM cbr_people_scores"),
    ("cbr_series_index", "EXPLAIN QUERY PLAN SELECT series_path, MIN(substr(date, 1, 4)) FROM cbr_people_scores WHERE series_path = ? GROUP BY series_path"),
    ("state_check", "EXPLAIN QUERY PLAN SELECT value FROM cbr_people_state WHERE key = ?"),
]

for name, q in queries:
    print(f"=== {name} ===")
    for row in con.execute(q, ("foo",) if "?" in q else ()).fetchall():
        print(" ", row)
'
```

### Step 2: Live Endpoint Benchmarking Under Auth
```bash
docker exec kometa python3 - << 'EOF'
import time, urllib.request
import kometa.auth as auth

token = auth.issue_token("me", "benchmark")
headers = {"Accept-Encoding": "gzip", "Authorization": f"Bearer {token}"}

endpoints = [
    ("Series Grid", "http://localhost:6969/api/series"),
    ("On Deck", "http://localhost:6969/api/ondeck"),
    ("Series Detail", "http://localhost:6969/api/series/381"),
    ("Related Row", "http://localhost:6969/api/series/381/related"),
    ("Queue Summary", "http://localhost:6969/api/queue/summary"),
]

try:
    for label, url in endpoints:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
            times.append((time.perf_counter() - t0) * 1000)
        print(f"{label:20s}: avg {sum(times)/len(times):6.2f} ms | size: {len(data)/1024:5.1f} KB")
finally:
    auth.revoke_token(token)
EOF
```

### Step 3: Pytest Duration Profiling
```bash
.venv/bin/python -m pytest tests/ --durations=10
```

---

## 4. Execution Roadmap

### Phase 1: Completed & Verified Live
- [x] Add `_reset_reader_activity` autouse fixture to `tests/conftest.py` (cuts test suite from 348s to 66s).
- [x] Add in-memory `_comm_memo` and `MAX(rowid)` / state check to `kometa/related_combined.py` (live: 0.4 ms memo, 1.1 ms fingerprint).
- [x] Eliminate slow pacing and live third-party network fetches in test suite (suite runtime down to 38.4s).
- [x] Pin community memoisation invalidation behavior in `tests/test_related_phase2.py`.
- [ ] Run one-time delete of legacy `metron:%` rows in `issue_details_cache` on production.

### Phase 2: First-Year Cache & Index Deprecation
- [ ] Add `first_year` column to `cbr_people_series` and populate it at harvest.
- [ ] Update `recommend()` in `kometa/cbr_people.py` to read `first_year` from `cbr_people_series`.
- [ ] Remove `CREATE INDEX IF NOT EXISTS idx_cbr_people_scores_series` from `cbr_people.py:87`.
- [ ] Drop `idx_cbr_people_scores_series` (reclaims 53 MB without query regression).

### Phase 3: `WITHOUT ROWID` Migration & Space Reclamation
- [ ] Add `scores_version` bumping in `kometa/cbr_people.py` (`record_issue_page`, `record_profile`).
- [ ] Implement one-time atomic table migration to `WITHOUT ROWID` with explicit column list in `cbr_people.ensure_tables()`.
- [ ] Run `VACUUM` via Python on production outside nightly backup window (shrinks DB from 328 MB $\to$ ~211 MB):
  `docker exec kometa python3 -c "import sqlite3; c = sqlite3.connect('/data/kometa.db', timeout=60); c.execute('VACUUM'); c.close()"`
