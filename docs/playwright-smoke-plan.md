# Playwright smoke suite — scope (drafted 2026-07-03, BUILT same session: tests/e2e/, 11 tests, ~11s)

Goal: the UI currently has zero regression coverage — every frontend fix this
week was verified by eyeball. A small, fast (<30s) browser smoke suite that
runs locally before deploys, catching "the view doesn't render / the button
does nothing / the modal is broken" class regressions. NOT a full E2E suite;
no external network, no real Komga/LOCG.

## Decisions (made — challenge tomorrow if they smell wrong)

1. **pytest-playwright (Python), not Playwright/Node.** One test ecosystem:
   the suite lives in the same pytest run as the existing 92 tests
   (`tests/e2e/`, deselectable via marker so `pytest -q` stays fast and the
   e2e pass is opt-in: `pytest -m e2e`). No node_modules in the repo.
   Dev deps: `pytest-playwright`, then `playwright install chromium`
   (~120MB, one-time, dev machine only — never on the NAS).

2. **Real backend on a fixture DB, not a mocked API.** A pytest fixture
   boots uvicorn in-process (same pattern as the smoke-boot checks used all
   through this branch: patch `db.DB_PATH` + `main/arcs/thumbnails.DB_PATH`
   to a tmp path, serve on 127.0.0.1:0). The app is designed to run keyless
   (no Komga/LOCG configured = no outbound calls from view routes), so a
   seeded DB gives real integration coverage with zero network.

3. **Seed via kometa.db, not SQL dumps.** A `seed()` helper in
   `tests/e2e/conftest.py` using `db.add_series` /
   `db.upsert_issue_status_many` / `db.queue_issue` — schema migrations stay
   the single source of truth, fixtures can't drift.

4. **Failure simulation via Playwright route interception**, not by killing
   the server: `page.route("**/api/series", abort)` to verify the
   renderView catch paints the error + Retry state. Also intercept
   `**/thumbnail` with a 1px PNG when a test needs "art present" tiles.

5. **Chromium only.** It's a self-hosted single-user app; cross-browser
   sweeps are not the risk.

## Smoke tests (v1 — roughly 10 tests, one file per view)

- `test_library.py`
  - empty DB → empty-state renders (no stuck "Loading…")
  - seeded (3 series, mixed owned/missing) → grid renders, counts right,
    filter chips work, search filters
  - API failure (route abort) → error + Retry button appears; Retry recovers
- `test_series_detail.py`
  - navigate from grid → title/meta/chips render; tabs switch (all/missing/
    upcoming/trades/arcs); detailTab does NOT leak when hopping series→series
  - issue tile → modal opens; Esc closes; reopen works (modal height reset)
  - keyboard: Tab to an issue tile, Enter opens the modal (the new a11y)
- `test_pull_list.py` — seeded upcoming issues → grouped This Week/Next
  Week/Later; pull row renders its ↓ button
- `test_activity.py` — seeded queue rows in mixed states → chips + labels
  from QUEUE_STATE; clear-history button exists and doesn't throw
- `test_wizard.py` — Add Series opens; typing fires the (debounced) search;
  backend keyless → intercept `**/api/search/**` with canned fixtures so
  results render; Esc closes. (Full add-flow is v2 — it mutates and syncs.)
- `test_settings.py` — renders all cards; a field edit fires the autosave
  PATCH (assert via request interception)

## Deliberately out of scope (v1)

- Real add/sync/download flows (external services; needs a mock-LOCG layer —
  v2 if ever)
- Visual/screenshot regression (flaky on fonts/animation; revisit only if
  layout regressions actually bite)
- Mobile viewport pass (cheap to add later: parametrize viewport)
- CI — there is no CI; the suite is a pre-deploy step. Wire into
  INSTRUCTIONS.md validation line once green: `pytest -m e2e` alongside
  ruff/pytest/node --check.

## Estimated shape

- `tests/e2e/conftest.py` (~120 lines: server fixture, seed helpers, marker)
- 6 test files, ~10 tests total
- Runtime target <30s headless
- Half-day including the inevitable fight with timing/waits (use Playwright
  auto-waiting + `expect()` polling; NEVER sleep())

## Open questions for tomorrow

1. Wizard fixtures: intercept at the browser (page.route, decision above) or
   add a `KOMETA_TEST_FIXTURES=1` backend mode? Browser interception keeps
   the backend untouched — start there.
2. Does the animated cover-fade / stagger cause flaky waits? If so, add a
   `prefers-reduced-motion` emulation to the browser context (the app
   already respects it in _animateRowOut).
