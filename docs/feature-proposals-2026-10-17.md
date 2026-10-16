# Feature Proposals and Codebase Quality of Life (2026-10-17)

A verified, vetted catalog of genuinely unimplemented features, reader modes, collection tools, and developer quality-of-life improvements for Kometa.

*(Note: End-of-issue next handoff with arc awareness, two-up double spread auto-centering, and the creator works shelf were audited and confirmed already built and live).*

---

## 1. Reader Enhancements (Unimplemented Reading Modes)

### 1.1 Continuous Vertical Scroll (Webtoon / Long-Strip Mode)
* **The Gap:** The reader (`kometa/static/reader.js`) only supports horizontal paging (`single` or `double` spread modes). For webcomics, modern digital releases, or reading on phones and portrait tablets, horizontal swiping is clunky compared to smooth vertical scrolling.
* **Proposal:** 
  * Add a `vertical` (Webtoon) layout mode alongside `single` and `double`.
  * Renders pages in a vertical column with minimal vertical gutters.
  * IntersectionObserver-based page progress tracking that updates the current page counter and scrub bar as you scroll.
  * Virtualized rendering (DOM recycling) to prevent memory ballooning on 60+ page graphic novels.
* **Implementation Seam:** `kometa/static/reader.js` (layout handler `_rdRender()`), CSS container in `kometa/static/style.css`.
* **Effort:** Moderate.

### 1.2 Right-to-Left (Manga / RTL) Reading Mode
* **The Gap:** `reader.js` currently assumes all books read left-to-right:
  - Swipe left always advances forward.
  - In two-up mode, page $N$ is always placed on the left and $N+1$ on the right (`rd-left`, `rd-right`).
  - For Japanese manga, this displays facing panels in reverse chronological order and breaks panel flow.
* **Proposal:**
  * Add an RTL toggle (`direction: 'ltr' | 'rtl'`) in reader controls.
  * In two-up mode with RTL enabled, reverse the pair order so page $N$ renders on the right and $N+1$ on the left.
  * Swipe/arrow navigation reversed (swipe right = next page).
  * Auto-detection from `ComicInfo.xml` (`<Manga>YesAndRightToLeft</Manga>`) or GCD tags (`Manga`).
* **Implementation Seam:** `kometa/static/reader.js` (`_rdBuildSpreads()` and key/touch handlers).
* **Effort:** Low.

---

## 2. Library & Collection Curation

### 2.1 Targeted "Interior Gap Filler" (vs. Full-Run Sweep)
* **The Gap:** The current `/api/series/{id}/search-missing` endpoint unconditionally queues *every* unowned historical issue in the run (e.g., clicking it on *Batman (1940)* where you own #608–619 queues 600+ back issues all the way to 1940).
* **Proposal:** 
  * Introduce an **"Interior Gaps"** mode that identifies missing issues strictly *bounded* between your lowest owned and highest owned issue numbers (e.g. if you own #1–10 and #12–20, it targets only issue #11).
  * Distinguish on the series page between:
    - **"Fill Gaps"** (interior holes in active reading runs — 1–3 issues).
    - **"Sweep All Missing"** (full run sweep).
* **Implementation Seam:** `kometa/db.py` (`get_interior_gaps(series_id)`) and `kometa/main.py`.
* **Effort:** Low.

### 2.2 Post-Trade Duplicate Archiver (Storage Reclaim)
* **The Gap:** `refresh_coverage()` correctly identifies when an acquired trade (like *Batman: Hush*) covers single issues (#608–619). However, if you already downloaded loose singles of those chapters earlier, both the trade and the individual `.cbz` files remain on disk indefinitely, wasting gigabytes of storage.
* **Proposal:** 
  * A dedicated **"Redundant Singles"** section in Tidy (or a series page action):
  * Scans for issues where `issue_status.owned == 1 AND covered_by IS NOT NULL`.
  * Displays: *"12 single issues are 100% covered by trades (1.8 GB reclaimable)"*.
  * 1-click **"Move Redundant Singles to Trash"** (utilizing Kometa's 7-day safe undo trash).
* **Implementation Seam:** Query in `kometa/tidy.py`, UI button in `kometa/static/app.js`.
* **Effort:** Low.

---

## 3. Acquisition & Queue Resilience

### 3.1 Manual Fulfill Dropzone (Magnet / NZB / File Paste)
* **The Gap:** When a release exhausts all three automated channels (Usenet, Torrent, GetComics) and lands in `failed` or `not_found`:
  - The user currently has to leave Kometa, search an external site, download the comic manually, move it via SMB/SSH into `/comics/...`, and run a library scan.
* **Proposal:** 
  * On any failed/unresolved item in the Activity queue, provide an action menu:
    - **"Paste Magnet / URL"**: Sends the magnet directly to qBittorrent under category `kometa`, binding it to the existing queue job.
    - **"Upload File"**: Drag & drop a `.cbz` directly onto the item row; Kometa verifies the archive and moves it directly into the destination folder.
* **Implementation Seam:** `kometa/acquisition.py`, endpoint `/api/queue/{id}/fulfill`, UI in `kometa/static/app.js`.
* **Effort:** Moderate.

### 3.2 Persistent Download Strike Override
* **The Gap:** Commit `5ee4750` introduced `source_strikes` (if a series has 2+ failures on Usenet within 30 days, it automatically routes future back issues to GetComics first). However, there is no UI indicator showing which series currently have active strikes, nor a way to reset them if an indexer repacked the releases.
* **Proposal:** 
  * On the series Acquisition tab, show a small badge if strikes are active (*"Usenet deprioritized: 3 recent bad releases"*).
  * Add a **"Reset Source Strikes"** button to allow retrying Usenet after indexer re-releases.
* **Implementation Seam:** Expose `source_strikes` in `GET /api/series/{id}`, add `DELETE /api/series/{id}/strikes`.
* **Effort:** Low.

---

## 4. Codebase & Developer Quality of Life

### 4.1 Fast In-Memory Test Suite (5.5m $\to$ 25s)
* **The Problem:** The pytest suite takes 5 minutes 46 seconds for 1,285 tests because tests create and sync physical `.db` files to disk on macOS storage.
* **Proposal:** 
  * Update the `db_path` test fixture in `tests/conftest.py` to use SQLite in-memory databases with URI connection sharing (`file:mem_{uuid}?mode=memory&cache=shared`) or a RAM-disk temp directory.
  * Allows running the entire 1,285-test suite in ~20–30 seconds, dramatically accelerating development and paired-agent iteration.
* **Effort:** Moderate (fixture refactoring in `tests/conftest.py`).

### 4.2 Centralized Path Traversal Sandbox (`docs/open-items.md:18`)
* **The Problem:** Listed in `docs/open-items.md` line 18: ensuring no filesystem operation can ever escape `/comics` or `/downloads`.
* **Proposal:** 
  * Create a centralized path sanitizer in `kometa/naming.py` or `kometa/security.py`:
    ```python
    def assert_within(path: str, allowed_roots: list[str]) -> str:
        real_p = os.path.realpath(path)
        if not any(os.path.commonpath([real_p, os.path.realpath(r)]) == os.path.realpath(r) for r in allowed_roots):
            raise PermissionError(f"Access denied: {path} outside allowed roots")
        return real_p
    ```
  * Call `assert_within` on all file-moving, deletion, trash, and extraction routines (`tidy.py`, `downloader.py`, `filing.py`).
* **Effort:** Low.

### 4.3 Activity Long-Polling / Differential Merging (`docs/open-items.md:26`)
* **The Problem:** When downloads are active, `app.js` polls the entire queue every 2 seconds (~6 KB gzipped JSON each time), causing continuous unneeded deserialization.
* **Proposal:** Support `GET /api/queue?since=<timestamp>` so only items whose status changed are returned, with the frontend updating its in-memory map.
* **Effort:** Low.
