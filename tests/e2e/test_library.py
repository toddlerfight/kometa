"""Library grid — render, the Monitored default, search, error/Retry."""
from playwright.sync_api import expect


def test_grid_defaults_to_monitored_and_all_shows_everything(app):
    # renderLibraryBrowse deliberately resets the filter to Monitored on every
    # entry — only pull-list series show by default. (The first draft of this
    # test assumed 'All' and the suite immediately taught us otherwise.)
    expect(app.locator(".series-card")).to_have_count(1)
    expect(app.locator(".series-card-title")).to_have_text("Test Comic Alpha")
    alpha = app.locator(".series-card", has_text="Test Comic Alpha")
    expect(alpha.locator(".series-card-count")).to_have_text("2/3")
    app.locator(".browse-filter-tab", has_text="All").click()
    expect(app.locator(".series-card")).to_have_count(3)


def test_search_filters_grid(app):
    app.locator(".browse-filter-tab", has_text="All").click()
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
    expect(page.locator(".series-card")).to_have_count(1)   # monitored default
