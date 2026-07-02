"""Add-series wizard — opens, searches (canned LOCG), closes clean."""
from playwright.sync_api import expect

CANNED_LOCG = '[{"id": 111, "name": "Ripcord", "source": "locg", "cover": "", "year_began": 2026, "publisher": {"name": "Ignition Press"}}]'


def test_wizard_search_renders_results(app):
    # Later route registrations win — this overrides the conftest empty.
    app.route("**/api/search/locg**",
              lambda r: r.fulfill(status=200, content_type="application/json", body=CANNED_LOCG))
    app.get_by_role("button", name="+ Add Series").click()
    box = app.locator("#wizard-search")
    expect(box).to_be_visible()
    box.fill("ripcord")
    result = app.locator(".wizard-result", has_text="Ripcord")
    expect(result).to_be_visible()
    app.keyboard.press("Escape")
    expect(app.locator("#modal")).to_be_hidden()
