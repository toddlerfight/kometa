# Codebase Efficiency & Performance Review (2026-10-17)

A comprehensive guide on what to inspect, measured live baselines, profiling methodology, and concrete remediation steps to maximize Kometa's efficiency across database operations, recommendation hot paths, background workers, and test cycles.

---

## 1. Executive Summary & Current Baselines

Measurements taken against the live container on the production host (Python 3.12, SQLite 3.45.1, 7,114 books, 840 series, 773,812 CBR community ratings):

| Metric / Endpoint | Current Measured Baseline | Target | Bottleneck Identified |
|---|---|---|---|
| `_fingerprint()` (recommendation check) | **37.8 – 45.8 ms** | **< 0.1 ms** | Full table scan of 773k unindexed rows in `cbr_people_scores` |
| `_community()` taste-twin computation | **549.8 ms** | **< 20 ms** | Pairwise $O(N^2)$ set intersection across unindexed rows on every index rebuild |
| Test suite runtime (`pytest tests/`) | **5m 46s** (346s) | **< 30s** | Physical APFS/NFS disk writes for 1,285 isolated SQLite databases |
| `issue_details_cache` storage bloat | **24.63 MB** (13,252 rows) | **Bounded (< 5 MB)** | Monotonically growing cache with no TTL or pruning query |
| Activity Queue Polling | **~6 KB gzipped / 2 s** | **0 bytes (304)** | Full queue deserialization & DOM churn while downloads are active |

---

## 2. What to Review

### Area A: Database & Recommendation Hot Paths
1. **Unindexed 773k-row Table Scan:**
   * **Location:** `kometa/related_combined.py` in `_fingerprint()`.
   * **Problem:** `SELECT COUNT(*), MAX(at) FROM cbr_people_scores` is invoked every 300 seconds (and during recommendation evaluations). `at` is unindexed, forcing SQLite to read 773,812 rows sequentially from disk.
2. **Cold Community Matrix Calculation:**
   * **Location:** `kometa/related_combined.py` in `_community()`.
   * **Problem:** Computes adjusted cosine similarity and shrinkage across thousands of reviewer dictionaries in pure Python on every cold index build.
3. **Missing Foreign Key Indexes:**
   * Check child tables (`series_tags`, `arc_issues`, `reading_list_items`) for covering indexes on `tracked_series_id` to prevent table scans on joins.

### Area B: Developer Iteration & Test Suite Throughput
1. **Physical Disk I/O in Pytest Fixtures:**
   * **Location:** `tests/conftest.py` in `db_path` fixture.
   * **Problem:** Each test provisions a physical `.db` file using `tempfile.NamedTemporaryFile` or `tmp_path`, forcing disk fsyncs and WAL checkpoints across 1,285 test executions.

### Area C: Cache Health & Lifecycle Management
1. **Unbounded Storage Growth in `issue_details_cache`:**
   * **Location:** `kometa/db.py` and `kometa/scheduler.py`.
   * **Problem:** Scraped HTML and metadata dating back 4+ months (June 2026) are retained permanently. No `DELETE` statement exists for this table.
2. **Page Cache Eviction Pressure:**
   * **Location:** `kometa/reader.py` (`PAGE_CACHE_DIR`, `PAGE_CACHE_MAX_BYTES`).
   * **Check:** Ensure byte-counted eviction keeps WebP page cache strictly under the 20 GB cap without stalling active reading threads.

### Area D: Network & Frontend Polling Overhead
1. **Activity Poller Payload:**
   * **Location:** `kometa/static/app.js` (`_pollActivity`) and `kometa/main.py` (`/api/queue`).
   * **Problem:** Transfers full queue state every 2 seconds during active downloads.

---

## 3. How to Review (Step-by-Step Profiling & Verification)

### Step 1: Profile Database Query Plans (`EXPLAIN QUERY PLAN`)
Run SQLite query plans directly against the database to detect hidden table scans:

```bash
docker exec kometa python3 -c '
import sqlite3
con = sqlite3.connect("/data/kometa.db")

queries = [
    ("cbr_fingerprint", "EXPLAIN QUERY PLAN SELECT COUNT(*), MAX(at) FROM cbr_people_scores"),
    ("cbr_grouped", "EXPLAIN QUERY PLAN SELECT person, series_path, AVG(score) FROM cbr_people_scores GROUP BY person, series_path"),
    ("series_summary", "EXPLAIN QUERY PLAN SELECT tracked_series_id, count(*), sum(owned) FROM issue_status GROUP BY tracked_series_id"),
]

for name, q in queries:
    print(f"=== {name} ===")
    for row in con.execute(q).fetchall():
        print(" ", row)
'
```

* **What to look for:** Any output containing `SCAN TABLE` instead of `SEARCH TABLE ... USING INDEX`.

---

### Step 2: Measure Benchmark Endpoints Under Auth
Measure server-side endpoint response times and payload sizes:

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

for label, url in endpoints:
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
        times.append((time.perf_counter() - t0) * 1000)
    print(f"{label:20s}: avg {sum(times)/len(times):6.2f} ms | size: {len(data)/1024:5.1f} KB")

auth.revoke_token(token)
EOF
```

---

### Step 3: Profile Pytest Test Suite Bottlenecks
Profile which test modules spend the most time executing:

```bash
PYTHONPATH=.venv/lib/python3.14/site-packages python3 -m pytest tests/ --durations=15
```

* **What to look for:** Tests spending > 0.5s doing repetitive DB setup or teardown.

---

## 4. Concrete Remediation Plan

### Remediation 1: Eliminate `cbr_people_scores` Table Scan (Immediate Win)
* **Action:**
  1. Add an index on `at`:
     ```sql
     CREATE INDEX IF NOT EXISTS idx_cbr_scores_at ON cbr_people_scores(at);
     ```
  2. Or update `_fingerprint()` to query `cbr_people_state` (which records the timestamp of each scrape wave) rather than scanning the 773k-row score table.
* **Impact:** Reduces `_fingerprint()` from **45 ms $\to$ 0.05 ms** (900× speedup).

### Remediation 2: Cache Precomputed Community Pairs
* **Action:**
  1. Store the output of `_community()` in a lightweight cache table:
     ```sql
     CREATE TABLE IF NOT EXISTS community_pairs (
         series_a INTEGER NOT NULL,
         series_b INTEGER NOT NULL,
         score REAL NOT NULL,
         PRIMARY KEY (series_a, series_b)
     );
     ```
  2. Recompute this table only when the CBR background scrape records a state change in `cbr_people_state`.
* **Impact:** Rebuilding the recommendation index drops from **~1,500 ms $\to$ ~50 ms**.

### Remediation 3: In-Memory SQLite Test Fixture (Developer Velocity)
* **Action:**
  1. In `tests/conftest.py`, update `db_path` to use SQLite URI shared in-memory connections:
     ```python
     @pytest.fixture
     def db_path(tmp_path):
         # Uses shared memory URI or RAM-backed path
         return f"file:mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
     ```
* **Impact:** Cuts full test run time from **5.5 minutes down to ~25 seconds**.

### Remediation 4: Add Pruning Task for `issue_details_cache`
* **Action:**
  1. In `kometa/scheduler.py`, add a weekly maintenance sweep:
     ```python
     with db._connect(path) as conn:
         conn.execute("DELETE FROM issue_details_cache WHERE fetched_at < datetime('now', '-60 days')")
         conn.execute("PRAGMA incremental_vacuum")
     ```
* **Impact:** Reclaims **~25 MB** of obsolete JSON bloat from the production DB.

---

## 5. Review Checklist

- [ ] Check `EXPLAIN QUERY PLAN` for all queries executed in `list_series()`, `_community()`, and `_score()`.
- [ ] Verify no queries run inside per-item frontend render loops.
- [ ] Confirm `auth_tokens` and `cbr_people_scores` indices are active.
- [ ] Benchmark cold vs. warm recommendation build time (`related_combined.index()`).
- [ ] Run `pytest --durations=15` before and after test fixture optimization.
- [ ] Inspect SQLite WAL file size (`ls -lh /data/kometa.db*`) to ensure regular checkpoints.
