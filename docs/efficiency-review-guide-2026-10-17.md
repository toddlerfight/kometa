# Codebase Efficiency & Performance Review (2026-10-17)

A verified, empirically measured guide on database optimization, test suite runtime, cache lifecycle, and recommendation hot paths for Kometa.

---

## 1. Executive Summary & Live Measured Baselines

All metrics measured against the live production database (Python 3.12, SQLite 3.45.1, 7,114 books, 840 series, 773,812 CBR community ratings across a 328 MB database):

| Area | Measured Baseline | Target | Root Cause / Verification | Action |
|---|---|---|---|---|
| **CBR Score Table Footprint** | **213 MB** (65% of entire DB) | **~75 – 80 MB** | Composite PK autoindex (67 MB) + unused series index (53 MB) | Migrate to `WITHOUT ROWID`, drop series index (reclaims ~135 MB) |
| **Fingerprint Scan** | **32 – 37 ms** (`COUNT(*), MAX(at)`) | **< 0.05 ms** | Sequential scan of 773k rows in `_fingerprint()`; runs every 20s–5m | Track `scores_version` in `cbr_people_state` or `MAX(rowid)` |
| **Community Matrix Build** | **310 ms** (195 ms GROUP BY, warm total 0.85s) | **0.0 ms** (warm) | Recalculated on issue record trickles; bulk of time is hub damping | Memoise `_community` in-memory keyed on score version |
| **Test Suite Execution** | **5m 48s** (348s across 1,285 tests) | **< 40s** | `kometa.activity._last_page["at"]` leaks from reader tests $\to$ 279s wasted waiting in `yield_to_reader()` | Add autouse fixture in `tests/conftest.py` resetting `_last_page` |
| **Cache Storage Bloat** | **33 MB** (13,252 rows) | **~28 MB** | 2,893 dead `metron:<id>` v1 keys (5.2 MB) unread since 2026-10-12 | One-time `DELETE FROM issue_details_cache WHERE key LIKE 'metron:%'` |
| **Activity Queue Polling** | **1 KB gzipped / 2 s** | **Optimal** | Already fixed on 2026-10-17 (`b0cde04`) via `/api/queue?since=` | Verified complete |
| **Secondary & Child Indexes** | **Optimal** | **Optimal** | `series_tags` PK leads with `tracked_series_id`; child tables correctly structured | No changes required |

---

## 2. In-Depth Findings & Architecture

### Area A: `cbr_people_scores` Storage Footprint (213 MB $\to$ ~78 MB)

The table `cbr_people_scores` and its indexes account for 65% of the database file:
- Main table B-tree: **~93 MB**
- Composite primary key auto-index (`person, series_path, number`): **~67 MB**
- Secondary index (`idx_cbr_people_scores_series`): **~53 MB**

#### 1. Is `idx_cbr_people_scores_series` Earning Its Keep?
**Verdict: No. It is virtually dead weight.**
* **Code Audit:** In the entire codebase, only one query filters on `series_path` without leading with `person`:
  ```python
  # kometa/cbr_people.py:718 in recommend()
  SELECT series_path, MIN(substr(date, 1, 4)) FROM cbr_people_scores
  WHERE series_path IN ({qs}) AND date LIKE '____-__-__' GROUP BY series_path
  ```
* **Impact:** This query is only called inside the standalone `/api/discover/community` experimental endpoint to discover publication years for the top 12 candidate runs when the year cannot be parsed from the slug.
* It does **not** assist `related_combined.py` (which runs `GROUP BY person, series_path` using the primary key index) nor harvest routines.
* **Remedy:** Drop `idx_cbr_people_scores_series` (reclaiming **53 MB** immediately). Publication years for candidate runs can be cached in `cbr_people_series` upon harvest.

#### 2. Migrating to `WITHOUT ROWID`
**Verdict: Eliminates the 67 MB duplicate auto-index.**
* SQLite normally stores table rows in a hidden 64-bit integer `rowid` B-tree and maintains an auxiliary B-tree for composite primary keys. Every insert duplicates the key columns.
* A `WITHOUT ROWID` table clusters data directly on `(person, series_path, number)`.
* **Benchmark (10,000 synthetic rows):**
  - Standard table + secondary index: **77.8 KB**
  - Standard table without secondary index: **61.4 KB**
  - `WITHOUT ROWID` without secondary index: **36.8 KB** (>50% size reduction)
* **The Catch:** `WITHOUT ROWID` tables do not have a `rowid` column (`SELECT MAX(rowid)` errors).
* **Solution:** Record a `scores_version` stamp in `cbr_people_state` whenever `record_profile` or `record_issue_page` commits scores. Checking this 4-row table takes **< 0.01 ms**.

---

### Area B: Test Suite Runtime & Module State Leakage (348s $\to$ ~40s)

* **Measured Reality:** Pytest disk I/O with `db.init_db()` takes only ~4 ms per test (~5s total across the suite).
* **The Actual Bottleneck:** Profiling with `--durations=15` revealed three tests taking 103 s, 90 s, and 86 s (279 of 348 total seconds).
* **Root Cause:** `kometa.activity._last_page["at"]` is module-level state. Reader tests set this timestamp when simulating page reads. Subsequent tests that execute background worker passes (such as `test_locg_sweep.py`) call `yield_to_reader()`, which sits in a 5-second sleep loop waiting out `READING_WINDOW_S` (90 s).
* **Fix:** Add an autouse fixture in `tests/conftest.py`:
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

---

### Area C: Recommendation Index & Community Memoisation

* **Measured Baseline:** `_community()` calculation takes 310 ms (195 ms for the `GROUP BY` query). A full warm index rebuild takes 0.85s, with the majority of time spent on hub damping.
* CBR scores only update during background CBR harvest windows, but issue additions or metadata updates trigger recommendation rebuilds.
* **Fix:** Memoise `_community()` in-memory in `related_combined.py` keyed on `(path, scores_version)` (similar to `_theme_memo` in `description_themes()`). Avoids disk schema changes while dropping redundant recalculation to 0.0 ms.

---

### Area D: `issue_details_cache` Lifecycle & Dead Key Pruning

* **Measured Baseline:** 13,252 rows (33 MB).
* **Safety Constraint:** Only 105 rows are older than 60 days. SQLite has `auto_vacuum = 0`, so age pruning does not shrink the file. Furthermore, `issue_meta` relies on stale `metron2:` entries as a graceful fallback when upstream Metron is unreachable ("old beats nothing").
* **True Dead Weight:** 2,893 legacy `metron:<id>` v1 rows (5.2 MB) unread since the `metron2:` migration on 2026-10-12.
* **Fix:** One-time prune of legacy keys:
  ```sql
  DELETE FROM issue_details_cache WHERE key LIKE 'metron:%';
  ```

---

## 3. Profiling & Measurement Procedures

### Step 1: Query Plan Inspection
Inspect query plans directly on the production container:
```bash
docker exec kometa python3 -c '
import sqlite3
con = sqlite3.connect("/data/kometa.db")

queries = [
    ("cbr_grouped_pk", "EXPLAIN QUERY PLAN SELECT person, series_path, AVG(score) FROM cbr_people_scores GROUP BY person, series_path"),
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
Safely benchmark live endpoints with guaranteed token revocation:
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

### Step 3: Test Suite Profiling
Run test suite duration checks using the virtualenv python:
```bash
.venv/bin/python -m pytest tests/ --durations=15
```

---

## 4. Execution Sequence

### Phase 1: Immediate Tactical Wins
- [ ] Add `_reset_reader_activity` autouse fixture to `tests/conftest.py` (cuts test suite from 348s to ~40s).
- [ ] Add in-memory `_community_memo` to `kometa/related_combined.py`.
- [ ] Execute one-time deletion of legacy `metron:%` rows in `issue_details_cache` on production.

### Phase 2: Database Footprint Reduction
- [ ] Drop `idx_cbr_people_scores_series` (reclaims 53 MB).
- [ ] Add `scores_version` update to `_put_score` / `record_profile` in `kometa/cbr_people.py`.
- [ ] Migrate `cbr_people_scores` to `WITHOUT ROWID` and run `VACUUM` (reclaims ~135 MB total).
