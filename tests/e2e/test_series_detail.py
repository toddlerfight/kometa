"""Series detail — tabs, issue modal, keyboard access, tab-leak regression."""
from playwright.sync_api import expect


def _open_alpha(app):
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(app.get_by_text("Test Comic Alpha").first).to_be_visible()


def test_detail_renders_and_tabs_switch(app):
    _open_alpha(app)
    expect(app.locator(".issue-tile")).to_have_count(5)          # ALL tab
    app.locator(".issue-tab", has_text="missing").click()
    expect(app.locator(".issue-tile")).to_have_count(1)          # just #3
    app.locator(".issue-tab", has_text="upcoming").click()
    expect(app.locator(".issue-tile")).to_have_count(2)          # #4, #5
    app.locator(".issue-tab", has_text="all").first.click()
    expect(app.locator(".issue-tile")).to_have_count(5)


def test_issue_modal_opens_and_esc_closes(app):
    _open_alpha(app)
    app.locator('.issue-tile[data-num="1"]').click()
    modal = app.locator("#modal")
    expect(modal).to_be_visible()
    expect(modal.get_by_text("#1")).to_be_visible()
    app.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    # Reopen — proves closeModal fully reset state (height pin, wide class)
    app.locator('.issue-tile[data-num="2"]').click()
    expect(modal).to_be_visible()
    expect(modal.get_by_text("#2")).to_be_visible()
    app.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_issue_tile_keyboard_access(app):
    # The 2026-07-02 a11y fix: tiles are tabbable and Enter opens the modal.
    _open_alpha(app)
    tile = app.locator('.issue-tile[data-num="3"]')
    tile.focus()
    app.keyboard.press("Enter")
    expect(app.locator("#modal")).to_be_visible()
    app.keyboard.press("Escape")


def test_detail_tab_does_not_leak_across_series(app):
    # Regression (fixed v=126): series->series hop via an Activity row used to
    # inherit the previous series' active tab.
    _open_alpha(app)
    app.locator(".issue-tab", has_text="missing").click()
    expect(app.locator(".issue-tile")).to_have_count(1)
    app.locator('.nav-item[data-view="activity"]').click()
    row = app.locator(".act-row", has_text="Gamma Run")
    expect(row).to_be_visible()
    row.locator(".act-row-meta").click()
    expect(app.get_by_text("Gamma Run").first).to_be_visible()
    expect(app.locator(".issue-tab", has_text="all").first).to_have_class(
        "issue-tab active")


# 1x1 transparent PNG — enough to fire the img load event that reveals v-cards.
_PX = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
_TWO_COVERS = (
    '{"covers": ['
    f'{{"id":"1","name":"Cover A","thumb":"{_PX}","large":"{_PX}"}},'
    f'{{"id":"2","name":"Cover B","thumb":"{_PX}","large":"{_PX}"}}'
    '], "selected_ids": []}'
)


def test_variant_card_click_zooms_instead_of_selecting(app):
    # Regression guard for the 2026-08 flow change: a grid card is a lightbox
    # trigger, never a selector. Include/★ live exclusively in the zoomed view.
    import re
    app.route("**/api/series/*/issues/*/variants", lambda route: route.fulfill(
        status=200, content_type="application/json", body=_TWO_COVERS))
    _open_alpha(app)
    app.locator('.issue-tile[data-num="1"]').click()
    app.locator("#imtab-variants").click()
    card = app.locator("#vc-1")
    expect(card).to_be_visible()
    card.click()
    expect(app.locator("#variant-lightbox")).to_be_visible()
    # The zoom itself must not have staged a selection.
    expect(card).not_to_have_class(re.compile(r"\bselected\b"))
    expect(app.locator("#variant-apply-btn")).to_be_disabled()
    # Choosing from the zoomed view drives the grid + Apply behind it.
    app.locator("#vlb-include").click()
    expect(card).to_have_class(re.compile(r"\bselected\b"))
    expect(app.locator("#variant-apply-btn")).to_be_enabled()
