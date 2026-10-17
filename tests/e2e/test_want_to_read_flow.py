"""Want to read: add from the issue's sheet, it's a row on On Deck, take it off again."""
from playwright.sync_api import expect


def test_want_to_read_from_the_sheet_to_on_deck_and_back(app, app_server):
    # Alpha #3 isn't on the shelf: its card opens the issue (no 'not here' label since 2026-10-17,
    # the owner's call: a missing entry wears its cover plain)
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    app.locator('.issue-tile[data-num="3"]').click()
    btn = app.locator("#modal .want-btn")
    expect(btn).to_be_enabled()
    expect(btn).to_have_text("Want to read")
    expect(btn.locator(".ico-check")).to_have_count(0)
    btn.click()
    expect(btn.locator(".ico-check")).to_have_count(1)                 # the tick, drawn (reader.js ico)
    expect(btn).to_have_text("Want to read")
    app.locator("#modal").get_by_role("button", name="Close").click()
    app.evaluate("navigate('ondeck')")
    row = app.locator(".od-row", has=app.locator(".od-head", has_text="Want to read"))
    card = row.locator(".od-card")
    expect(card).to_have_count(1)
    expect(card.locator(".od-tag")).to_have_count(0)
    card.click()                                                    # off again from the same sheet: the row goes
    btn = app.locator("#modal .want-btn")
    expect(btn.locator(".ico-check")).to_have_count(1)
    btn.click()
    expect(btn.locator(".ico-check")).to_have_count(0)
    expect(btn).to_have_text("Want to read")
    app.locator("#modal").get_by_role("button", name="Close").click()
    app.evaluate("navigate('library'); navigate('ondeck')")
    expect(app.locator(".od-head", has_text="Want to read")).to_have_count(0)
