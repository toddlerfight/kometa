# Codebase Efficiency & Performance Review (2026-10-17)

A verified, empirically measured guide on database optimization, test suite runtime, cache lifecycle, and recommendation hot paths for Kometa.

---

## 1. Executive Summary & Live Measured Baselines

All metrics measured against the live production database (Python 3.12, SQLite 3.45.1, 7,114 books, 840 series, 773,812 CBR community ratings across a 328 MB database) and container runtime:

| Area | Measured Baseline | Target | Root Cause / Verification | Action |
|---|---|---|---|---|
| **CBR Score Table Footprint** | **213 MB** (65% of entire DB) | **101.4 MB** | Composite PK autoindex (67 MB) + series index (53 MB) + table (101.4 MB) | Cache first-year in `cbr_people_series`, migrate to `WITHOUT ROWID`, drop series index (saves 112 MB) |
| **Fingerprint Scan** | **32 – 37 ms** (`COUNT(*), MAX(at)`) | **< 0.05 ms** | Sequential scan of 773k rows in `_fingerprint()`; runs every 20s–5m | Track `MAX(rowid)` or `scores_version` in `cbr_people_state` |
| **Community Matrix Build** | **310 ms** (195 ms GROUP BY, warm total 0.85s) | **0.0 ms** (warm) | Recalculated on issue record trickles; bulk of time is hub damping | Memoise `_community` in-memory keyed on `(cbr_stamp, linked)` |
| **Test Suite Execution** | **348 s** (1,294 tests) | **66 s** (Phase 1) $\to$ **~14 s** (Phase 2) | Reader clock leakage (`activity._last_page["at"]` = 279s wait in `yield_to_reader()`) + 8 inline timeout tests (52s) | Added `_reset_reader_activity` in `conftest.py` (drops 279s); mock rescue/limiter sleeps in the 8 tests |
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
* **Clean Solution:** Store each run's `first_year` directly in `cbr_people_series` (a 2-column table of ~3k rows) during harvest, and read it from there. Once `cbr_people_series` serves the year, drop `idx_cbr_people_scores_series` safely without causing a 67 ms table scan on discover loads.

#### 2. Migrating to `WITHOUT ROWID`
* **Real Production Copy Measurement:**
  - Standard table + series index: **213 MB**
  - `WITHOUT ROWID` + series index: **157.4 MB** (secondary indexes in `WITHOUT ROWID` store the full composite primary key instead of a 64-bit integer rowid)
  - `WITHOUT ROWID` without series index: **101.4 MB** (**saves 112 MB**, reducing the table footprint by more than half)
  - Query speed: `GROUP BY person, series_path` accelerates from **189 ms down to 129 ms**.
* **Migration Procedure ("The How"):**
  1. Guarded one-time migration step inside `cbr_people.ensure_tables()`:
     ```sql
     -- Check sqlite_master for existing schema
     CREATE TABLE cbr_people_scores_v2 (
         person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL,
         score REAL NOT NULL, date TEXT, via TEXT, at TEXT,
         PRIMARY KEY (person, series_path, number)
     ) WITHOUT ROWID;
     INSERT OR REPLACE INTO cbr_people_scores_v2 SELECT * FROM cbr_people_scores;
     DROP TABLE cbr_people_scores;
     ALTER TABLE cbr_people_scores_v2 RENAME TO cbr_people_scores;
     ```
     This copy takes ~1.0 s in an atomic transaction.
  2. Invalidate cache via `cbr_people_state`:
     Since `WITHOUT ROWID` removes the `rowid` column, `cbr_people.py` bumps a `scores_version` key in `cbr_people_state` once per page or profile commit.
  3. Reclaim physical disk space:
     Run `VACUUM` at the next deploy window via `./deploy.sh` (outside nightly backup windows) to shrink the database file.

---

### Area B: Test Suite Runtime (348s $\to$ 66s $\to$ ~14s)

#### 1. The Reader Clock Fixture (348s $\to$ 66s)
* **Root Cause:** `activity._last_page["at"]` is module state. Reader tests set it, and subsequent tests invoking `yield_to_reader()` wait in 5-second sleep loops until the 90s `READING_WINDOW_S` expires (279s wasted across 3 tests).
* **Fix Applied:** Autouse fixture in `tests/conftest.py`:
  ```python
  @pytest.fixture(autouse=True)
  def _reset_reader_activity():
      from kometa import activity
      activity._last_page["at"] = 0.0
      activity._book_pages.clear()
      yield
      activity._last_page["at"] = 0.0
      activity._book_pages.clear()
  ```
  All 1,294 tests pass cleanly in **66 seconds**.

#### 2. The Next 8 Slow Tests (52s)
The remaining 1,286 tests run in just 14 seconds. The top 8 slow tests account for 52 seconds:
- `test_webtoon_parks_with_a_clear_error_and_cleans_the_staging` (15.5 s): Triggered by `_rescue_later` executing synchronously under `FINALIZE_INLINE=True`, attempting GetComics search across real network timeouts.
- `test_three_failures_in_a_row_is_the_wall_and_the_pages_are_not_blamed` (10.1 s): Unmocked `fi` fallback in `sweep_tick` call 2 calling real `locg_client.fill_issue_details` across a 10s timeout.
- `test_owned_on_disk...` (6.0 s), `test_extra_runs_merge...` (5.7 s), `test_builds_issue_list_from_locg_anon` (4.7 s), `test_a_broken_page_is_skipped...` (4.0 s), `test_a_locg_trade_landing...` (3.8 s), `test_flaresolverr_clears...` (2.3 s): Real limiter spacing and backoff sleeps not routed through injectable mock sleeps.

---

### Area C: Recommendation Index & Community Memoisation

* **Baseline:** `_community()` takes 310 ms (195 ms `GROUP BY`).
* **Fix Applied:** In-memory dictionary `_comm_memo` in `kometa/related_combined.py` keyed on `(cbr_stamp, linked)`.
* Redundant rebuilds triggered by issue trickles or title changes return in **0.0 ms**.
* `_fingerprint()` queries `MAX(rowid)` (or `scores_version` from `cbr_people_state`), dropping check latency from 37 ms to **< 0.05 ms**.

---

### Area D: `issue_details_cache` Dead Key Pruning

* **Table Structure:** Key column is `locg_issue_id` (not `key`).
* **Dead Weight:** 2,893 legacy `metron:<id>` v1 entries (5.2 MB) from pre-October 2026.
* **Targeted Prune:**
  ```sql
  -- Verify count first:
  SELECT COUNT(*) FROM issue_details_cache WHERE locg_issue_id LIKE 'metron:%';
  -- Delete legacy keys:
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
.venv/bin/python -m pytest tests/ --durations=15
```

---

## 4. Execution Roadmap

### Phase 1: Completed / In Progress
- [x] Add `_reset_reader_activity` autouse fixture to `tests/conftest.py` (cuts test suite from 348s to 66s).
- [x] Add in-memory `_comm_memo` and `MAX(rowid)` / state check to `kometa/related_combined.py`.
- [ ] Run one-time delete of legacy `metron:%` rows in `issue_details_cache` on production.

### Phase 2: First-Year Cache & Index Deprecation
- [ ] Add `first_year` column to `cbr_people_series` and populate it at harvest.
- [ ] Update `recommend()` in `kometa/cbr_people.py` to read `first_year` from `cbr_people_series`.
- [ ] Drop `idx_cbr_people_scores_series` (reclaims 53 MB without query regression).

### Phase 3: `WITHOUT ROWID` Migration & Space Reclamation
- [ ] Add `scores_version` bumping in `kometa/cbr_people.py` (`record_issue_page`, `record_profile`).
- [ ] Implement one-time atomic table migration to `WITHOUT ROWID` in `cbr_people.ensure_tables()`.
- [ ] Run `VACUUM` on production during deploy window to shrink database from 328 MB $\to$ ~190 MB.
