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
    page = reader.locator(".rd-page")
    expect(page).to_have_count(1)
    expect(page).to_have_attribute("alt", "Page 1")
    app.wait_for_function("document.querySelector('#reader .rd-page').naturalWidth > 0")

    app.keyboard.press("ArrowRight")
    app.keyboard.press("ArrowRight")
    expect(reader.locator(".rd-page")).to_have_attribute("alt", "Page 3")

    book_id = int(app.url.split("book=")[1])
    app.wait_for_function(
        f"fetch('/api/books/{book_id}').then(r=>r.json()).then(b=>b.progress && b.progress.page===3)",
        polling=300, timeout=6000)

    app.keyboard.press("Escape")
    expect(reader).to_be_hidden()
    expect(app.get_by_text("Test Comic Alpha").first).to_be_visible()
    # Leave no trace for the next test. Exiting saved page 3 with a fresh
    # timestamp, so an older-stamped reset would be refused as stale — by design.
    import sqlite3
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress WHERE book_id = ?", (book_id,))


def test_landscape_pairs_pages_but_cover_and_spread_stand_alone(app):
    app.set_viewport_size({"width": 1180, "height": 820})          # iPad landscape: two-up
    _open_alpha_issue_1(app)
    app.locator("#modal").get_by_role("button", name="Read").click()
    reader = app.locator("#reader")
    expect(reader.locator(".rd-page")).to_have_count(1)            # cover alone
    app.keyboard.press("ArrowRight")
    expect(reader.locator(".rd-page")).to_have_count(2)            # 2–3 paired
    app.keyboard.press("ArrowRight")
    expect(reader.locator(".rd-page")).to_have_count(1)            # 4 is a wide spread: alone
    expect(reader.locator(".rd-page")).to_have_attribute("alt", "Page 4")
    app.keyboard.press("Escape")
