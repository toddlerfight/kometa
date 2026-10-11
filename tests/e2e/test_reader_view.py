"""Reader smoke (docs/reader-spec.md step 1): Read button → pages → progress → back."""
import re

from playwright.sync_api import expect


def _open_alpha_issue_1(app):
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    app.locator('.issue-tile[data-num="1"]').click()
    expect(app.locator("#modal")).to_be_visible()


def test_read_button_opens_reader_turns_pages_and_saves_progress(app, app_server):
    app.set_viewport_size({"width": 820, "height": 1180})          # iPad portrait: one-up
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    reader = app.locator("#reader")
    expect(reader).to_be_visible()
    expect(app).to_have_url(re.compile(r"#read\?book=\d+"))
    page = reader.locator("#rd-stage .rd-page")
    expect(page).to_have_count(1)
    expect(page).to_have_attribute("alt", "Page 1")
    app.wait_for_function("document.querySelector('#reader #rd-stage .rd-page').naturalWidth > 0")

    app.keyboard.press("ArrowRight")
    app.keyboard.press("ArrowRight")
    expect(reader.locator("#rd-stage .rd-page")).to_have_attribute("alt", "Page 3")

    book_id = int(app.url.split("book=")[1])
    app.wait_for_function(
        f"fetch('/api/books/{book_id}').then(r=>r.json()).then(b=>b.progress && b.progress.page===3)",
        polling=300, timeout=6000)

    app.keyboard.press("Escape")
    expect(reader).to_be_hidden()
    expect(app.get_by_text("Test Comic Alpha").first).to_be_visible()
    # Leave no trace for the next test. Exiting saved page 3 with a fresh
    # timestamp, so an older-stamped reset would be refused as stale — by design.
    # The save is debounced (1.2s): let any pending write land BEFORE wiping, or
    # it lands after and the next test opens at page 3 instead of the cover.
    app.wait_for_timeout(1600)
    import sqlite3
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress WHERE book_id = ?", (book_id,))


def test_landscape_pairs_pages_but_cover_and_spread_stand_alone(app):
    app.set_viewport_size({"width": 1180, "height": 820})          # iPad landscape: two-up
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    reader = app.locator("#reader")
    expect(reader.locator("#rd-stage .rd-page")).to_have_count(1)            # cover alone
    app.keyboard.press("ArrowRight")
    expect(reader.locator("#rd-stage .rd-page")).to_have_count(2)            # 2–3 paired
    app.keyboard.press("ArrowRight")
    expect(reader.locator("#rd-stage .rd-page")).to_have_count(1)            # 4 is a wide spread: alone
    expect(reader.locator("#rd-stage .rd-page")).to_have_attribute("alt", "Page 4")
    app.keyboard.press("Escape")


def test_read_shows_the_reader_at_once_while_a_slow_book_opens(app, app_server):
    import time
    def slow(route):
        time.sleep(1.5)                                  # a first open off a busy NAS
        route.continue_()
    app.route("**/api/series/*/issues/1/book", slow)
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    expect(app.locator("#reader .rd-loading .rd-load-fill")).to_be_visible(timeout=500)   # before the book answers
    expect(app.locator("#reader #rd-stage .rd-page")).to_be_visible(timeout=10000)
    expect(app.locator("#reader #rd-loading")).to_have_count(0, timeout=5000)            # gone once page 1 shows
    assert "coming off" not in app.locator("#reader").inner_text()
    app.unroute("**/api/series/*/issues/1/book")


def test_a_trackpad_swipe_turns_one_page_per_gesture(app, app_server):
    app.set_viewport_size({"width": 820, "height": 1180})          # one-up
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    page = app.locator("#reader #rd-stage .rd-page")
    expect(page).to_have_count(1)
    start = int(page.get_attribute("alt").split()[-1])               # an earlier test may have saved a place
    at = lambda n: expect(page).to_have_attribute("alt", f"Page {n}")
    app.mouse.move(410, 590)
    for _ in range(8):                                               # one gesture, momentum and all
        app.mouse.wheel(30, 0)
    at(start + 1)
    app.wait_for_timeout(400)                                        # the gesture goes quiet
    app.mouse.wheel(0, 200)                                          # vertical: not a page turn
    app.wait_for_timeout(100)
    at(start + 1)
    app.mouse.wheel(-90, 0)                                          # the other way: back a page
    at(start)


def test_a_quick_second_trackpad_swipe_counts_and_a_diagonal_one_turns(app, app_server):
    app.set_viewport_size({"width": 820, "height": 1180})
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    page = app.locator("#reader #rd-stage .rd-page")
    expect(page).to_have_count(1)
    start = int(page.get_attribute("alt").split()[-1])               # an earlier test may have saved a place
    at = lambda n: expect(page).to_have_attribute("alt", f"Page {n}")
    app.mouse.move(410, 590)
    for d in (30, 26, 20, 14, 9, 6, 4):                              # a swipe and its decaying momentum
        app.mouse.wheel(d, 0)
    at(start + 1)
    for d in (28, 30, 22):                                           # a fresh push inside the tail
        app.mouse.wheel(d, 0)
    at(start + 2)
    app.wait_for_timeout(300)
    for _ in range(6):                                               # wobbly: sideways wins over the whole swipe
        app.mouse.wheel(-12, 9)
    at(start + 1)
