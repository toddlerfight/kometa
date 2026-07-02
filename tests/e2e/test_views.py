"""Pull list, Activity, Settings — each view renders its seeded data."""
from playwright.sync_api import expect


def test_pull_list_groups_upcoming(app):
    app.locator('.nav-item[data-view="pull-list"]').click()
    expect(app.locator(".pull-group-label").first).to_be_visible()
    row = app.locator(".pull-row", has_text="Test Comic Alpha")
    expect(row.first).to_be_visible()          # issue #4 is on the pull list


def test_activity_shows_queue_states(app):
    # Terminal states land in the Completed section as .act-row (in-flight
    # ones are .act-card) — the seed is all-terminal so boot recovery and the
    # queue processor leave it alone.
    app.locator('.nav-item[data-view="activity"]').click()
    expect(app.locator(".act-row")).to_have_count(2)
    expect(app.get_by_text("Failed").first).to_be_visible()
    expect(app.get_by_text("Not Found").first).to_be_visible()


def test_settings_renders_and_autosaves(app):
    app.locator('.nav-item[data-view="settings"]').click()
    field = app.locator("#f-comics-root")
    expect(field).to_be_visible()
    # The autosave REALLY saves — this test mutates shared session config, so
    # the new value must be a directory that exists or comics_root_ok flips
    # false and the wizard test (which runs after) gets the setup screen.
    with app.expect_request(
        lambda r: "/api/config" in r.url and r.method == "PATCH", timeout=10000
    ):
        field.fill("/tmp")
        field.blur()
