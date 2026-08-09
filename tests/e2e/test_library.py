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
    # + Missing → union pulls gamma (its one issue is unowned) in beside alpha
    app.locator(".browse-filter-tab", has_text="Missing").click()
    expect(app.locator(".series-card")).to_have_count(2)
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
