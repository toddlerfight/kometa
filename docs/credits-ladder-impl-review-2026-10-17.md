# Implementation Review: Credits Ladder (2026-10-17)

References:
- Design: `docs/credits-sourcing-design-2026-10-17.md`
- Design Review: `docs/credits-sourcing-review-2026-10-17.md`
- Dry Run Report: `docs/credits-dry-run-2026-10-17.md`
- Review Request: `docs/credits-ladder-review-prompt-2026-10-17.md`
- Codebase: commit `4214715` ("Credits ladder (switch off) and Illustrator counted as an artist")

---

## 1. Verdict

**Ready after named changes.**

The core architecture, non-blending invariant, role mapping, and switch gating in commit `4214715` are cleanly designed and solidly implemented. The test suite passes 100% (8/8 ladder tests; 1,428/1,428 overall suite).

However, switching the ladder on immediately would trigger an **infinite re-query loop on every trickle tick** due to a rank check omission in `credits_wanted`. Once that bug (Finding 1) is resolved and the cover-only GCD fall-through rule (Decision 1) is addressed, the ladder is ready for production.

---

## 2. Findings

### Finding 1: `credits_wanted` re-selects already-satisfied GCD rows on every tick (Bug / High Severity)
* **Location:** `kometa/record.py:521-523`
* **What's wrong:**
  In `credits_wanted`:
  ```python
  ids = [n for n, k in (("gcd", "gcd_id"), ("comicvine", "cv_id")) if e.get(k) or (n == "comicvine" and r.get("cv_issue_id"))]
  if any(not tried.get(n) or _age_days(tried[n]) >= CREDIT_RUNG_RETRY_DAYS for n in ids):
      out.append(r)
  ```
  When an issue with `source in ('locg', 'comicinfo')` has both a `gcd_id` and a `cv_id`, `fill_credits` queries GCD first. When GCD returns credits, the loop breaks (`out.update(source='gcd'); break`). Comic Vine is never queried, so `tried.get('comicvine')` remains unset (`None`).
  On the very next trickle tick (10 minutes later), `credits_wanted` scans the row again. Because `not tried.get('comicvine')` is `True`, it includes the row in `out`. `fill_credits` is called, observes `rank == 4` (GCD), sees `CREDIT_RANK['comicvine'] <= 4`, and breaks immediately without doing anything.
  **Result:** Every such row will be selected by `credits_wanted` and run through `fill_credits` as a no-op on **every single tick forever**.
* **Confirmation:** Confirmed by static code analysis and reproducing with a synthesized row in Python.
* **Fix:** In `credits_wanted`, filter `ids` to only those rungs whose rank strictly exceeds the row's current credit rank:
  ```python
  rank = CREDIT_RANK.get(credits_source(r), 0) if r.get("credits") else 0
  ids = [n for n, k in (("gcd", "gcd_id"), ("comicvine", "cv_id"))
         if (e.get(k) or (n == "comicvine" and r.get("cv_issue_id"))) and CREDIT_RANK[n] > rank]
  if ids and any(not tried.get(n) or _age_days(tried[n]) >= CREDIT_RUNG_RETRY_DAYS for n in ids):
      out.append(r)
  ```

---

### Finding 2: Cover-only GCD entries preempt full creative teams (Design Gap / Medium Severity)
* **Location:** `kometa/record.py:444-460` and `docs/credits-dry-run-2026-10-17.md`
* **What's wrong:**
  In the dry run, GCD has only cover credits for `Deadpool Corps: Rank and Foul #1` (2 cover artists, 0 writers, 0 artists) and `Hit-Monkey #1` (1 cover artist, 0 writers, 0 artists). Meanwhile, Comic Vine has the complete interior creative team (Hit-Monkey: Daniel Way [Writer], Dalibor Talajić [Artist], Joe Sabino [Letterer]).
  Under strict ranking, GCD wins, and the comic sheet is populated with cover artists only, losing the actual writer and artist.
* **Confirmation:** Confirmed from the dry run report (`docs/credits-dry-run-2026-10-17.md`, lines 40 & 47).
* **Fix:** When GCD returns a list containing *neither a writer nor an artist* (i.e. only covers or miscellaneous credits), treat it as an incomplete catalogue entry and fall through to Comic Vine if Comic Vine has a writer or artist (see Decision 1 below).

---

### Finding 3: Comic Vine API calls lack rate limiting / budget guard (Risk / Medium Severity)
* **Location:** `kometa/record.py:416-426`
* **What's wrong:**
  While GCD queries are gated behind `gcd_client.budget().get("left", 0) <= 0`, Comic Vine queries in `_ask_comicvine` call `cv._get` directly. Comic Vine strictly enforces 200 requests/hour per resource.
  In the background trickle, 6 issues every 10 minutes equals a maximum of 36 calls/hour, which is within the 200/hr limit. However, `details()` also invokes `fill_credits` asynchronously on user interaction (`kometa/record.py:845`). A user rapidly browsing issues or a burst of arc lookups could trigger rate limit errors (HTTP 420/429).
* **Confirmation:** Verified by reviewing `kometa/comicvine_client.py` (which contains no internal call throttle) and `kometa/record.py:420-426`.
* **Fix:** Add a sliding-window hourly limiter or check against `health.record` before querying Comic Vine, ensuring that interactive requests cannot starve background arc syncing.

---

### Finding 4: `credits_wanted` matches `"gcd_id": null` in SQL (Nit / Low Severity)
* **Location:** `kometa/record.py:513`
* **What's wrong:**
  `r.extra_json LIKE '%"gcd_id": %'` matches both `"gcd_id": 12345` and `"gcd_id": null`. Python filters it out later via `if e.get(k)`, so no bad API calls occur, but the SQL query retrieves unnecessary candidate rows into memory.
* **Confirmation:** Verified against SQLite string matching rules.
* **Fix:** Use `r.extra_json LIKE '%"gcd_id": [0-9]%'` or `(r.extra_json LIKE '%"gcd_id": %' AND r.extra_json NOT LIKE '%"gcd_id": null%')`.

---

### Finding 5: Missing role aliases in `_cv_credits` (Nit / Low Severity)
* **Location:** `kometa/record.py:391`
* **What's wrong:**
  In `_cv_credits`:
  `names = {"writer": "Writer", "script": "Writer", "plotter": "Plot", "penciler": "Penciller", "penciller": "Penciller", "artist": "Artist", "inker": "Inker", "colorist": "Colorist", "letterer": "Letterer", "cover": "Cover"}`
  Comic Vine occasionally uses `"illustrator"` or `"pencils"`. While rare, they will currently be ignored and dropped as unknown roles.
* **Confirmation:** Code inspection of `kometa/record.py:391-396`.
* **Fix:** Add `"illustrator": "Artist"` and `"pencils": "Penciller"` to `names`.

---

## 3. Detailed Audit against "What to Check"

### 1. Invariants in `_credits_to_keep` and `fill_credits`
* **One catalogue's list, never blended:** Verified. In `fill_credits`, the loop breaks immediately upon receiving a non-empty list from a rung. In `_credits_to_keep`, the function returns either `held` or `incoming` as an atomic list.
* **Metron > GCD > Comic Vine > LOCG > file tags:** Verified. `CREDIT_RANK = {"metron": 5, "gcd": 4, "comicvine": 3, "locg": 2, "comicinfo": 1}`.
* **Higher replaces lower; lower never replaces higher:** Verified. In `_credits_to_keep`:
  `if held and (not incoming or CREDIT_RANK.get(held_from, 0) > CREDIT_RANK.get(source, 0)):` keeps `held`.
* **Empty answer never clears a list:** Verified. In `_credits_to_keep`, `not incoming` preserves `held`. In `fill_credits`, if all rungs return empty, `out["source"]` is `None`, and `credits_json` is not updated.
* **Direct writers of `credits_json`:** Verified across the entire codebase. Only two places write to `credits_json`:
  1. `kometa/record.py:330` in `write_issue` (guarded by `_credits_to_keep`).
  2. `kometa/record.py:473` in `fill_credits` (guarded by rank check).
  *(Line 976 is an initial placeholder insert for variants when no issue row exists at all).*

### 2. Switch Off Behavior
* `credits_ladder_on(path)` reads `db.get_config(path).get("credits_ladder") == "on"`.
* Verified all call sites:
  - `credits_tick` (line 494): returns `0` immediately.
  - `details` (line 845): thread is only spawned if `credits_ladder_on(path)` is true.
  - `_safe_refresh` (line 861): `fill_credits` only called if `credits_ladder_on(path)` is true.
  - `write_issue` (line 314): `_credits_to_keep` is always active, providing passive protection against data clearing even with the ladder off.
* **Result:** With `credits_ladder` off, there is zero external network activity and zero behavior regression.

### 3. Provenance & Retry Tracking
* `field_sources_json` is clean and backwards-compatible. For existing untouched rows, it is `NULL`, defaulting to `{}` in `_row()`.
* When filled, `fs["credits"] = {"source": out["source"], "at": _now()}` is recorded.
* If Metron later refreshes with an empty answer, `_credits_to_keep` returns `sources` and `write_issue` updates `field_sources_json`, preserving the GCD / Comic Vine provenance.
* `credits_tried`: If a rung answers empty (`[]`), `tried[name] = _now()` is recorded and retried only after `CREDIT_RUNG_RETRY_DAYS` (7 days). If a rung could not be asked (`None` due to missing ID or exhausted budget), it is not recorded in `tried`, allowing immediate retry once the prerequisite is met.

### 4. Budgets and Limits
* **GCD:** Properly checked via `gcd_client.budget().get("left", 0) <= 0`. Daily cap of 1,800 is fully respected.
* **Comic Vine:**
  - Trickle worst case: 6 issues / tick $\times$ 6 ticks / hour = 36 calls/hour.
  - 36 calls/hour is 18% of Comic Vine's 200/hr limit.
  - While safe in background isolation, lack of an hourly guard creates burst risk during interactive sheet browsing (see Finding 3).

### 5. Downstream Systems
* **Creator ID Linking:** Confirmed. Names in GCD and Comic Vine credits arrive with `metron_creator_id=None`. When read by the UI or recommendation engine, `related.with_creator_ids()` matches them against the local shelf by name, resolving IDs and spawning background lookups for unknown names.
* **Retagging:** Confirmed. When `credits_json` changes, `comicinfo.for_book()` generates updated XML, producing a new digest that `retag.requeue_changed()` detects.
* **ComicInfo Notes Line:** Naming the source in the `Notes` element of `ComicInfo.xml` is not yet built. **It should NOT block switching on.** Tagging files with actual creator credits now is vastly superior to waiting for a cosmetic attribution string.

### 6. Test Suite Coverage
* The 8 new tests in `tests/test_credits_ladder.py` are well-isolated and cover rank comparison, fall-through, dry run mode, role transformation, and switch gating.
* **Recommended additional tests:**
  1. Test that `credits_wanted` does not return a row whose credits are already at GCD rank (verifying the fix for Finding 1).
  2. Test that `_ask_gcd` returns `None` (not `[]`) when `gcd_client.budget()` has 0 left.
  3. Test that Comic Vine credits survive a subsequent Metron refresh that returns an empty credit list.

---

## 4. Answers to the Three Decisions

### Decision 1: Short GCD list (covers only) against fuller Comic Vine list
* **Decision: Allow fall-through to Comic Vine when GCD has neither a writer nor an artist.**
* **Rationale:** In `Deadpool Corps: Rank and Foul #1` and `Hit-Monkey #1`, GCD's volunteers had only indexed the cover sequence. Ranking GCD strictly above Comic Vine resulted in showing only cover artists and losing the entire interior writing and art team.
* **Policy Rule:** A credits list lacking both a `Writer` and an `Artist` is not a complete credits list. Falling through to Comic Vine in this specific scenario preserves the non-blending rule (the resulting list still comes 100% from Comic Vine) while avoiding severely degraded metadata on the issue sheet.

### Decision 2: Long lists from anthologies and collected editions (Judge Dredd: 52 lines)
* **Decision: Store the full list in `issue_record.credits_json`; truncate/group only in DISPLAY (UI).**
* **Rationale:**
  1. **Relational Graph:** The creator pages, "More from" rows, and CBR recommendation signals read `issue_record.credits_json`. If credits are truncated in storage, every creator past the cutoff is severed from the graph.
  2. **Tagging Completeness:** `ComicInfo.xml` should hold the full list of creators for a collected edition.
  3. **Display Solution:** The issue sheet / modal should display the primary contributors (Writers, Artists) and collapse the remainder behind a `+ N more creators` expander.

### Decision 3: Ready to switch on?
* **Verdict: Not ready until Finding 1 is patched; ready immediately after.**
* **Required action before switching `credits_ladder = "on"`:**
  1. Patch `credits_wanted` in `kometa/record.py:521` to filter out rungs where `CREDIT_RANK[n] <= rank` (preventing the infinite tick loop).
  2. Add unit test asserting `credits_wanted` ignores rows where `CREDIT_RANK[credits_source(row)] >= 4`.
  3. Enable `credits_ladder = "on"` in production config and monitor the first 24 hours of trickle fills.
