# Feature Proposals and Codebase Quality of Life (2026-10-17)

A prioritized catalog of proposed features, reading enhancements, library curation workflows, and developer quality-of-life improvements for Kometa.

---

## 1. Reader & Reading Continuum (Everyday Reading Flow)

### 1.1 End-of-Issue Next Handoff & Arc Progression
* **Problem:** When finishing an issue or collected edition in the web reader, the reader hits the end and stops. The reader must close the modal, locate the next issue on the series page, and open it.
* **Proposal:**
  * When reaching the last page of a comic, render an end-card overlay: **"Next: Issue #N"** (or next collected volume) with an immediate **"Read Next"** button that seamless-loads the next book into the reader without round-tripping to the library view.
  * **Story Arc Awareness:** If the issue is part of an active Story Arc (e.g. *Batman #497* in *Knightfall*), the prompt detects the reading order and surfaces: *"Next in Knightfall: Detective Comics #664"*, allowing readers to follow cross-series events without manual catalog navigation.
* **Implementation Seam:** `kometa/static/reader.js` + an API endpoint `/api/book/{id}/next` querying `issue_status` and `arc_issues` in `kometa/db.py`.
* **Effort:** Moderate.

### 1.2 Double-Page Spread Detection & Two-Up Alignment
* **Problem:** In two-up landscape mode, digital comics with splash pages or double-page spreads stored as a single wide image (aspect ratio $\ge 1.15$) break subsequent page alignment, putting left and right facing pages permanently out of sync for the remainder of the issue.
* **Proposal:** Auto-detect wide image dimensions on decode. When an image has an aspect ratio $\ge 1.15$, display it centered across the viewport as a single spread, resetting the alternating left/right grid for subsequent pages.
* **Implementation Seam:** `kometa/static/reader.js`.
* **Effort:** Low.

---

## 2. Collector & Library Intelligence

### 2.1 "Fill Gaps" (Interior Missing Issues)
* **Problem:** Long runs often have small holes (e.g., owning #1–14 and #16–25, but missing #15). Currently, collectors must click into #15 and trigger a search manually, or turn on the pull list for the entire run.
* **Proposal:** A **"Fill Gaps"** button on the series page that detects *interior* gaps—unowned issues bounded between the lowest and highest owned issue numbers. Clicking the button queues search/acquisition only for those specific missing issues, ignoring early historical backlog or future uncollected tails.
* **Implementation Seam:** Helper `db.get_interior_gaps(series_id)` in `kometa/db.py` wired to an action button in `kometa/static/app.js`.
* **Effort:** Low.

### 2.2 Creator Filmography / Library Explorer Drawer
* **Problem:** Readers frequently find an author or artist they enjoy (e.g. *Brian K. Vaughan*, *Fiona Staples*, *Jeff Lemire*) and want to see everything by that creator across their collection.
* **Proposal:** Clicking any creator tag opens a slide-over drawer:
  1. **In Your Library:** All series and issues featuring that creator, grouped by their role (Writer, Artist, Cover).
  2. **Top Recommended Works:** Highest-rated unowned series in the catalogue created by them.
* **Implementation Seam:** Leverage the existing creator indexing from `kometa/related_combined.py` and `series_signals`.
* **Effort:** Moderate.

### 2.3 Post-Trade Acquisition Cleanup Prompt
* **Problem:** When an omnibus or trade paperback is added (e.g., *Batman: Hush* covering #608–619), previous loose single issues of those same chapters remain in the folder.
* **Proposal:** When `kometa.tradefill.refresh_coverage()` marks previously owned single issues as covered by an acquired trade, display an action toast or badge on the series page: *"12 singles now covered by Trade. Move loose files to Trash / Archive?"*
* **Implementation Seam:** Query `issue_status.owned == 1 AND covered_by IS NOT NULL` in `kometa/tidy.py`.
* **Effort:** Low.

---

## 3. Recommendation & Discovery

### 3.1 Home Discovery Rails: Creator Spotlights & Themes
* **Problem:** The Home view currently features "Because you read...", but lacks broader contextual shelves.
* **Proposal:** Add dynamic discovery rails on the main dashboard:
  - **Creator Spotlight:** *"More from Brian K. Vaughan"* or *"Art by Greg Capullo"*.
  - **Genre / Theme Spotlight:** *"Post-Apocalyptic Highlights"* or *"Leading Ladies in Fantasy"*.
  - **Taste Twin Badge:** Highlight books with strong community support using an affinity chip (e.g., *"94% reader affinity"* on *Seven to Eternity* for *Monstress*).
* **Implementation Seam:** Expose pre-grouped rails in `kometa/related.py` and render carousel cards in `kometa/static/app.js`.
* **Effort:** Moderate.

### 3.2 Blind Community Card Auditing Tool
* **Problem:** Standalone community-only recommendation cards (`related_community = cards`) are parked because raw reader co-ratings can measure release-era popularity rather than pure taste affinity.
* **Proposal:** A lightweight internal audit tool (`/tools/community-audit`) presenting 20 random community-only pairs side-by-side with simple Thumbs Up / Thumbs Down actions.
  - Enables the owner to score 50 candidate pairs in 2–3 minutes.
  - If $\ge 85\%$ are approved, `related_community = cards` can be safely enabled permanently.
* **Implementation Seam:** An admin route using `related_combined.related_combined(..., opts={'community': 'cards'})`.
* **Effort:** Low (~100 lines of template/JS).

---

## 4. Codebase & Developer Quality of Life

### 4.1 Fast In-Memory Test Suite (5.5m $\to$ 20s)
* **Problem:** Running the full test suite (`pytest tests/`) takes 5 minutes 46 seconds (1,285 tests) because hundreds of test cases create and teardown physical SQLite DB files on macOS storage.
* **Proposal:** Configure test fixtures in `tests/conftest.py` to use SQLite shared in-memory databases (`file:memdb_...?mode=memory&cache=shared`) or a RAM-backed temporary directory (`/tmp/kometa_ramfs`).
* **Expected Benefit:** Dramatically cuts developer and paired-agent iteration loops from minutes to seconds.
* **Effort:** Moderate.

### 4.2 Path Fencing & Sandboxing Guard
* **Problem:** Security open-item (`docs/open-items.md:18`) requires ensuring folder operations never escape the library or downloads root.
* **Proposal:** A centralized path verification function:
  ```python
  def assert_within_root(path: str, root: str) -> str:
      resolved_path = os.path.realpath(path)
      resolved_root = os.path.realpath(root)
      if os.path.commonpath([resolved_root, resolved_path]) != resolved_root:
          raise PermissionError(f"Path traversal attempted: {path} outside {root}")
      return resolved_path
  ```
  Enforce this check across delete, rename, and file-filing routes in `kometa/tidy.py` and `kometa/downloader.py`.
* **Effort:** Low.

### 4.3 Download Rescue Dashboard
* **Problem:** Releases that exhaust all three acquisition channels (Usenet, Torrent, GetComics) sit in the queue in a `failed` or `not_found` state.
* **Proposal:** Add an "Unresolved" tab in Activity showing failed items with one-click quick actions:
  - **Alternate Title Search:** Try alias or publisher-prefixed variations.
  - **Manual Dropzone:** One-click paste magnet link or upload NZB/CBZ directly to fulfill that specific queue item.
* **Implementation Seam:** `kometa/acquisition.py` + `kometa/static/app.js`.
* **Effort:** Moderate.
