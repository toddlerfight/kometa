"""Adding a series — from the Library's one box (2026-11-12; the "+ Add Series"
button is gone), and the wizard modal the FIND buttons still open. Canned sources."""
from playwright.sync_api import expect

CANNED_LOCG = '[{"id": 111, "name": "Ripcord", "source": "locg", "cover": "", "year_began": 2026, "publisher": {"name": "Ignition Press"}}]'


def _locg(app, body=CANNED_LOCG, status=200):
    # Later route registrations win — this overrides the conftest empty.
    app.route("**/api/search/locg**",
              lambda r: r.fulfill(status=status, content_type="application/json", body=body))


def _catalogue_calls(app):
    calls = []
    app.on("request", lambda r: any(f"/api/search/{s}" in r.url for s in ("locg", "metron", "comicvine", "storyline"))
           and calls.append(r.url))
    return calls


def test_no_add_series_button(app):
    expect(app.locator(".series-card").first).to_be_visible()
    expect(app.get_by_role("button", name="+ Add Series")).to_have_count(0)
    expect(app.locator("#browse-search")).to_have_attribute("placeholder", "Search or add a series…")


def test_not_on_the_shelf_looks_in_the_catalogue_and_offers_add(app):
    _locg(app)
    app.locator("#browse-search").fill("ripcord")
    expect(app.get_by_text("Not on your shelf")).to_be_visible()
    row = app.locator(".cat-row", has_text="Ripcord")
    expect(row).to_be_visible()                       # nobody pressed anything: the pause did it
    expect(app.locator("#cat-head")).to_have_text("In the catalogue · 1")
    row.get_by_role("button", name="Add").click()
    expect(app.get_by_role("button", name="Track Series")).to_be_visible()
    expect(app.locator("#modal .wizard-result-title")).to_contain_text("Ripcord")
    app.get_by_role("button", name="Back").click()    # no list behind this pick: Back just closes
    expect(app.locator("#modal")).to_be_hidden()
    expect(row).to_be_visible()                       # and the results are still on the page


def test_on_the_shelf_never_rings_the_catalogue_unasked(app):
    _locg(app)
    calls = _catalogue_calls(app)
    app.locator("#browse-search").fill("beta")
    expect(app.locator(".series-card")).to_have_count(1)
    find = app.locator(".cat-find")
    expect(find).to_contain_text("Find “beta” in the catalogue")
    app.wait_for_timeout(1500)                        # well past the pause
    assert calls == []
    find.click()
    expect(app.locator("#cat-head")).to_have_text("In the catalogue · 1")
    assert len(calls) == 4                            # one round: Metron, LOCG, Comic Vine, storylines
    expect(app.locator(".series-card")).to_have_count(1)   # the shelf stays where it was


def test_enter_asks_the_catalogue_at_once(app):
    _locg(app)
    box = app.locator("#browse-search")
    box.fill("beta")
    box.press("Enter")
    expect(app.locator(".cat-row", has_text="Ripcord")).to_be_visible()


def test_typing_on_sends_only_the_words_left_in_the_box(app):
    """Bedford Street, 2026-11-06: 'bed' went out first and hogged LOCG's one line."""
    _locg(app)
    calls = _catalogue_calls(app)
    app.locator("#browse-search").press_sequentially("ripcord", delay=90)
    expect(app.locator(".cat-row", has_text="Ripcord")).to_be_visible()
    assert [c for c in calls if "/locg" in c] == [c for c in calls if c.endswith("locg?q=ripcord")]
    assert len(calls) == 4


def test_already_on_the_shelf_says_so_instead_of_add(app):
    _locg(app, '[{"id": 222, "series": "Beta Saga", "source": "locg", "cover": "", "year_began": 2023, "publisher": {"name": "DC Comics"}},'
               ' {"id": 223, "series": "Beta Saga", "source": "locg", "cover": "", "year_began": 1999, "publisher": {"name": "DC Comics"}}]')
    box = app.locator("#browse-search")
    box.fill("beta saga")
    box.press("Enter")
    rows = app.locator(".cat-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("On your shelf")              # same name, same year
    expect(rows.nth(0).get_by_role("button", name="Add")).to_have_count(0)
    expect(rows.nth(1)).to_contain_text("same name on shelf")         # same name, another year: still addable
    expect(rows.nth(1).get_by_role("button", name="Add")).to_be_visible()


def test_retries_a_busy_locg_and_finds_the_run(app):
    """LOCG busy on the first knock used to read as 'nothing found'."""
    calls = []

    def locg(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(status=503, content_type="application/json", body='{"detail": "LOCG is busy"}')
        else:
            route.fulfill(status=200, content_type="application/json", body=CANNED_LOCG)
    app.route("**/api/search/locg**", locg)
    app.locator("#browse-search").fill("ripcord")
    expect(app.locator(".cat-row", has_text="Ripcord")).to_be_visible(timeout=10000)
    assert len(calls) == 2


def test_says_when_locg_never_answers_and_knocks_again(app):
    _locg(app, '{"detail": "LOCG is busy"}', 503)
    app.locator("#browse-search").fill("ripcord")
    expect(app.get_by_text("LOCG didn’t answer")).to_be_visible(timeout=15000)
    expect(app.locator("#cat-head")).to_have_text("Not in the catalogue either")
    _locg(app)
    app.get_by_role("button", name="Try again").click()
    expect(app.locator(".cat-row", has_text="Ripcord")).to_be_visible()


def test_wizard_modal_still_opens_for_find_buttons(app):
    """Related / creator pages hand a name to the wizard modal (showAddWizard(name))."""
    _locg(app)
    expect(app.locator(".series-card").first).to_be_visible()
    app.evaluate("showAddWizard('ripcord')")
    expect(app.locator("#wizard-search")).to_have_value("ripcord")
    expect(app.locator("#modal .wizard-result", has_text="Ripcord")).to_be_visible()
    app.keyboard.press("Escape")
    expect(app.locator("#modal")).to_be_hidden()
