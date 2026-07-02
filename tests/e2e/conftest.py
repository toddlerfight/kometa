"""E2E smoke fixtures — real backend, fixture DB, zero external network.

The server boots ONCE per session (in-process uvicorn, same pattern as the
smoke-boot checks in the deploy flow) against a tmp SQLite DB seeded through
kometa.db helpers, so the schema migrations stay the single source of truth.

Network quarantine, two layers:
- BACKEND: the app runs keyless (no Komga/LOCG/CV config) and the seed avoids
  the fields that trigger anonymous LOCG calls (no locg_issue_id on issues,
  fresh last_synced so the frontend's stale auto-sync never fires, a
  far-future last_full_sync so the startup catch-up stays asleep).
- BROWSER: routes that WOULD reach the outside world through the backend
  (search, arcs discovery, trades, variants) are intercepted per-page with
  canned responses. Tests can re-route with page.route to simulate richer
  data or failures — last registration wins in Playwright.
"""
import threading
import time
import urllib.request

import pytest

E2E_PORT = 6971
BASE = f"http://127.0.0.1:{E2E_PORT}"


def _seed(db, path):
    """A small, deliberate catalog. Everything mark_synced'd so the UI's
    self-healing auto-sync (>1h stale) never fires mid-test."""
    from datetime import date, timedelta
    today = date.today()
    past = lambda d: str(today - timedelta(days=d))
    future = lambda d: str(today + timedelta(days=d))

    alpha = db.add_series(title="Test Comic Alpha", publisher="Image",
                          year_began=2024, folder_path=None, on_pull_list=True, path=path)
    db.upsert_issue_status_many([
        (alpha, 1.0, past(300), True,  None, None, None),
        (alpha, 2.0, past(200), True,  None, None, None),
        (alpha, 3.0, past(100), False, None, None, None),   # missing
        (alpha, 4.0, future(3), False, None, None, None),   # upcoming (pull list)
        (alpha, 5.0, future(40), False, None, None, None),  # upcoming, later
    ], path=path)
    db.mark_synced(alpha, path)

    beta = db.add_series(title="Beta Saga", publisher="DC Comics",
                         year_began=2023, folder_path=None, on_pull_list=False, path=path)
    db.upsert_issue_status_many([
        (beta, 1.0, past(400), True, None, None, None),
        (beta, 2.0, past(370), True, None, None, None),
    ], path=path)
    db.mark_synced(beta, path)

    gamma = db.add_series(title="Gamma Run", publisher="Image",
                          year_began=2025, folder_path=None, on_pull_list=False, path=path)
    db.upsert_issue_status_many([
        (gamma, 1.0, past(50), False, None, None, None),
    ], path=path)
    db.mark_synced(gamma, path)

    # Activity: one row per interesting state. TERMINAL states only —
    # lifespan's reset_stuck_queue_items flips searching/downloading back to
    # queued at boot (that reset ate the first version of this seed), and
    # queued rows invite the interval queue-processor to actually process.
    db.queue_issue(alpha, 3.0, path)
    db.queue_issue(gamma, 1.0, path)
    rows = {(r["tracked_series_id"], r["issue_number"]): r["id"] for r in db.get_queue(path)}
    db.update_queue_state(rows[(alpha, 3.0)], "failed", error="e2e seed", path=path)
    db.update_queue_state(rows[(gamma, 1.0)], "not_found", error="No result on GetComics, Usenet or torrent", path=path)

    # Keep the startup catch-up asleep — this suite never wants a full sync.
    # comics_root must be a REAL directory or the add-wizard renders the
    # first-run setup screen instead of search.
    import os as _os
    root = _os.path.join(_os.path.dirname(path), "comics")
    _os.makedirs(root, exist_ok=True)
    db.set_config({"last_full_sync": "9999-01-01 00:00:00", "comics_root": root}, path)

    return {"alpha": alpha, "beta": beta, "gamma": gamma}


@pytest.fixture(scope="session")
def app_server(tmp_path_factory):
    """Boot the real app once for the whole e2e session."""
    dbfile = str(tmp_path_factory.mktemp("e2e") / "kometa-e2e.db")

    import kometa.db as db
    db.DB_PATH = dbfile
    # Every route module snapshots DB_PATH at import — patch them ALL or a
    # background thread quietly writes to /data/kometa.db (which doesn't exist
    # on a dev machine; that exact bug was the "pre-existing" test failures).
    import kometa.main as main
    import kometa.arcs as arcs
    import kometa.thumbnails as thumbnails
    import kometa.sync as sync
    import kometa.sources as sources
    import kometa.acquisition as acquisition
    for mod in (main, arcs, thumbnails, sync, sources, acquisition):
        mod.DB_PATH = dbfile

    db.init_db(dbfile)
    ids = _seed(db, dbfile)

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=E2E_PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/api/config", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("e2e app server never came up")

    yield {"base": BASE, "ids": ids, "db_path": dbfile}
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # The app respects prefers-reduced-motion — kill the cover-fade staggers
    # and row animations so waits never race a transition.
    return {**browser_context_args, "reduced_motion": "reduce"}


@pytest.fixture
def app(app_server, page):
    """Page wired to the app with the network quarantine applied."""
    def canned(body):
        return lambda route: route.fulfill(status=200, content_type="application/json", body=body)

    # Backend routes that reach the outside world (anonymous LOCG/CV/Wikipedia)
    # get canned empties; individual tests override with their own page.route.
    page.route("**/api/search/**", canned("[]"))
    page.route("**/api/search/storyline**", canned("[]"))
    page.route("**/api/series/*/arcs", canned('{"arcs": []}'))
    page.route("**/api/series/*/trades", canned('{"trades": [], "cached": true}'))
    page.route("**/api/series/*/issues/*/variants", canned('{"covers": [], "selected_ids": []}'))
    page.route("**/api/series/*/issues/*/locg-details", canned('{"desc": "", "credits": []}'))

    page.goto(f"{app_server['base']}/")
    return page


def pytest_collection_modifyitems(items):
    # Everything under tests/e2e/ is an e2e smoke — mark it so the default
    # run (addopts -m "not e2e") skips the browser suite.
    for item in items:
        if "/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
