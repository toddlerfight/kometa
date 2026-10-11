"""The reader bar and thumbnail strip draw over the page, never under it."""
def test_chrome_over_page(app, app_server):
    app.set_viewport_size({"width": 1180, "height": 820})
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    app.locator('.issue-tile[data-num="1"]').click()
    app.locator("#modal").get_by_role("button", name="Read").click()
    app.wait_for_function("document.querySelector('#reader #rd-stage .rd-page') && document.querySelector('#reader #rd-stage .rd-page').naturalWidth > 0")
    app.evaluate("_rdToggleChrome(true)")
    app.wait_for_timeout(400)
    strip = app.locator("#rd-strip").bounding_box()
    x, y = strip["x"] + strip["width"] / 2, strip["y"] + strip["height"] / 2
    top = app.evaluate(f"document.elementFromPoint({x}, {y}).closest('#rd-chrome') !== null")
    assert top, "the page is drawn over the reader bar"
