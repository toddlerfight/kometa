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


def test_wizard_retries_a_busy_locg_and_finds_the_run(app):
    """Bedford Street, 2026-11-06: LOCG busy on the first knock used to read as
    'nothing found'. The wizard now knocks again and the run shows up."""
    calls = []

    def locg(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(status=503, content_type="application/json", body='{"detail": "LOCG is busy"}')
        else:
            route.fulfill(status=200, content_type="application/json", body=CANNED_LOCG)
    app.route("**/api/search/locg**", locg)
    app.get_by_role("button", name="+ Add Series").click()
    app.locator("#wizard-search").fill("ripcord")
    expect(app.locator(".wizard-result", has_text="Ripcord")).to_be_visible(timeout=10000)
    assert len(calls) == 2


def test_wizard_says_when_locg_never_answers(app):
    app.route("**/api/search/locg**",
              lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail": "LOCG is busy"}'))
    app.get_by_role("button", name="+ Add Series").click()
    app.locator("#wizard-search").fill("ripcord")
    expect(app.get_by_text("LOCG didn’t answer")).to_be_visible(timeout=15000)
