"""Library grid — render, the everything-default + attention toggles, search,
error/Retry."""
from playwright.sync_api import expect


def test_grid_defaults_to_all_and_toggles_narrow(app):
    # Since eb57cdb the browse view has no Monitored tab: everything shows by
    # default, and Upcoming/Missing are independent toggle chips. Both on is a
    # UNION (needs-attention view), not an intersection.
    expect(app.locator(".series-card")).to_have_count(3)
    alpha = app.locator(".series-card", has_text="Test Comic Alpha")
    expect(alpha.locator(".series-card-count")).to_have_text("2/3")
    # Upcoming → alpha alone (#4/#5 are future); beta complete, gamma has no dates ahead
    app.locator(".browse-filter-tab", has_text="Upcoming").click()
    expect(app.locator(".series-card")).to_have_count(1)
    expect(app.locator(".series-card-title")).to_have_text("Test Comic Alpha")
    # + Missing → still just alpha: gamma has an unowned issue but isn't on the
    # pull list, and a gap you haven't asked for isn't "missing" (2026-10-08)
    app.locator(".browse-filter-tab", has_text="Missing").click()
    expect(app.locator(".series-card")).to_have_count(1)
    # Both back off → everything again
    app.locator(".browse-filter-tab", has_text="Upcoming").click()
    app.locator(".browse-filter-tab", has_text="Missing").click()
    expect(app.locator(".series-card")).to_have_count(3)


def test_search_filters_grid(app):
    expect(app.locator(".series-card")).to_have_count(3)
    app.locator("#browse-search").fill("beta")
    expect(app.locator(".series-card")).to_have_count(1)
    expect(app.locator(".series-card-title")).to_have_text("Beta Saga")
    app.locator("#browse-search").fill("zzz-no-match")
    expect(app.get_by_text("No series match.")).to_be_visible()


def test_api_failure_paints_retry_and_recovers(app_server, page):
    # Break /api/series BEFORE first paint — the view must land on the error
    # state (not stuck "Loading..."), and Retry must actually recover.
    page.route("**/api/series", lambda r: r.abort())
    page.goto(f"{app_server['base']}/")
    expect(page.get_by_text("Couldn't load this view.")).to_be_visible()
    retry = page.get_by_role("button", name="Retry")
    expect(retry).to_be_visible()
    page.unroute("**/api/series")
    retry.click()
    expect(page.locator(".series-card")).to_have_count(3)   # everything-default


def _canned_series(page, base, rows):
    import json
    page.route("**/api/series", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(rows)))
    page.route("**/api/shelf", lambda r: r.fulfill(status=200, content_type="application/json", body='{"series": [], "tracked_reading": {}}'))
    page.goto(f"{base}/#library")


def _row(i, title, **kw):
    base = {"id": i, "title": title, "publisher": "Dark Horse Comics", "owned": 4, "missing": 0, "upcoming": 0, "calendar_date": None,
            "on_pull_list": False, "favourite": False, "family_parent": None, "family_children": [], "metron_type": "Limited Series",
            "match_status": "auto", "franchise": None, "newest_file_at": None}
    base.update(kw)
    return base


def test_a_franchise_is_one_stack_card_that_opens_to_its_members(app_server, page):
    fr = {"key": "aliens", "name": "Aliens", "count": 5}
    rows = [_row(i, f"Aliens - Part {i}", franchise=fr) for i in range(1, 6)] + [_row(9, "Zed Solo")]
    _canned_series(page, app_server["base"], rows)
    expect(page.locator(".series-card")).to_have_count(2)                     # one stack + one loose series
    stack = page.locator(".stack-card")
    expect(stack.locator(".series-card-title")).to_have_text("Aliens")
    expect(stack.locator(".series-card-count")).to_have_text("5 series")
    stack.click()
    expect(page.locator("#topbar-title")).to_have_text("Aliens")
    expect(page.locator(".series-card")).to_have_count(5)                     # the stack's page: its members, flat
    page.go_back()
    page.locator(".browse-filter-tab", has_text="Stacks").click()            # Stacks off → everything flat
    expect(page.locator(".series-card")).to_have_count(6)
    page.locator("#browse-search").fill("part 3")                             # a search looks inside stacks
    expect(page.locator(".series-card")).to_have_count(1)
    expect(page.locator(".series-card-title")).to_have_text("Aliens - Part 3")


def test_runs_chip_hides_single_issue_series(app_server, page):
    rows = [_row(1, "Long Run", owned=12), _row(2, "A One-Shot", owned=1, metron_type="One-Shot"),
            _row(3, "Lone GN", owned=1, metron_type=None)]
    _canned_series(page, app_server["base"], rows)
    expect(page.locator(".series-card")).to_have_count(3)
    page.locator(".browse-filter-tab", has_text="Runs").click()
    expect(page.locator(".series-card")).to_have_count(1)
    expect(page.locator(".series-card-title")).to_have_text("Long Run")


def test_alphabetical_sort_gets_letter_headers_and_a_rail(app_server, page):
    rows = [_row(1, "The Walking Dead"), _row(2, "Batman"), _row(3, "[1992] Aliens - Platinum"), _row(4, "100 Bullets"), _row(5, "Wytches")]
    _canned_series(page, app_server["base"], rows)
    page.locator("#sort-alpha").click()
    expect(page.locator(".letter-head")).to_have_count(4)                      # #, A, B, W
    expect(page.locator(".letter-head").first).to_have_text("#")
    expect(page.locator(".letter-rail .letter-rail-btn")).to_have_count(4)
    titles = page.locator(".series-card-title").all_inner_texts()
    assert titles == ["100 Bullets", "[1992] Aliens - Platinum", "Batman", "The Walking Dead", "Wytches"]   # 'The' ignored
    page.locator("#sort-date").click()
    expect(page.locator(".letter-head")).to_have_count(0)
    expect(page.locator("#letter-rail")).to_have_count(0)
