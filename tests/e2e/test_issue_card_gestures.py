"""The issue card, the same everywhere (2026-10-17): an issue you have reads on a tap
and shows its card on the ⋯; one you don't shows its card on a tap. The card's
actions are the same wherever it opens; Go to series only off the run's own page."""
import re
import sqlite3

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _alpha(app):
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(app.locator('.issue-tile[data-num="1"]')).to_be_visible()
    return int(re.search(r"id=(\d+)", app.url).group(1))


def test_a_tap_on_an_issue_you_have_reads_it(app, app_server):
    _alpha(app)
    app.locator('.issue-tile[data-num="1"]').click()
    expect(app).to_have_url(re.compile(r"#read\?book=\d+"))           # straight in: no card on the way
    expect(app.locator("#reader .rd-page").first).to_be_visible()
    expect(app.locator("#modal")).to_be_hidden()
    app.keyboard.press("Escape")
    expect(app.locator("#reader")).to_be_hidden()
    app.wait_for_timeout(1600)                                         # leave no trace: the open stamps page 1
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress")


def test_the_dots_on_an_issue_you_have_open_its_card(app):
    _alpha(app)
    app.locator('.issue-tile[data-num="1"] .od-menu').click()
    modal = app.locator("#modal")
    expect(modal).to_be_visible()
    for name in ("Want to read", "Mark as read", "Read"):
        expect(modal.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(modal.locator(".mark-row")).to_be_visible()                 # rating and heart
    expect(modal.get_by_text(re.compile(r"\d+ pages"))).to_be_visible()
    expect(modal.get_by_role("button", name="Go to series")).to_have_count(0)   # already here
    expect(modal.get_by_role("button", name="Close")).to_have_count(0)
    app.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_a_tap_on_an_issue_you_dont_have_opens_its_card(app):
    _alpha(app)
    app.locator('.issue-tile[data-num="3"]').click()
    modal = app.locator("#modal")
    expect(modal).to_be_visible()
    expect(modal.get_by_text("Missing", exact=True)).to_be_visible()
    for name in ("Want to read", "Ignore", "Download"):
        expect(modal.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(modal.get_by_role("button", name="Read", exact=True)).to_have_count(0)
    expect(modal.locator(".mark-row")).to_have_count(0)                # rating and heart: yours only
    app.keyboard.press("Escape")


def test_off_its_page_the_card_offers_the_series(app):
    sid = _alpha(app)
    app.evaluate("navigate('ondeck')")
    app.evaluate(f"showIssueModal({sid}, 4)")
    modal = app.locator("#modal")
    expect(modal.get_by_text("Upcoming", exact=True)).to_be_visible()
    expect(modal.get_by_text(re.compile(r"\d+ days? away"))).to_be_visible()
    modal.get_by_role("button", name="Go to series").click()
    expect(app).to_have_url(re.compile(rf"#series-detail\?id={sid}"))
    expect(modal).to_be_hidden()


def test_in_the_reader_the_dots_are_the_card(app, app_server):
    app.set_viewport_size({"width": 1180, "height": 820})              # landscape: pages pair
    _alpha(app)
    app.locator('.issue-tile[data-num="1"]').click()
    expect(app.locator("#reader .rd-page").first).to_be_visible()
    app.evaluate("_rdToggleChrome(true)")
    expect(app.locator("#reader").get_by_role("button", name="Shift pairing by one")).to_be_visible()
    app.locator("#reader").get_by_role("button", name="Issue details").click()
    modal = app.locator("#modal")
    expect(modal).to_be_visible()
    for name in ("Want to read", "Go to series", "Mark as read"):
        expect(modal.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(modal.get_by_role("button", name="Read", exact=True)).to_have_count(0)   # you're in it
    expect(modal.locator(".mark-row")).to_be_visible()                 # the heart lives here now
    modal.get_by_role("button", name="Mark as read", exact=True).click()
    expect(modal).to_be_hidden()
    expect(app.locator("#reader .rd-end")).to_be_visible()             # the reader's own Mark as read: the end screen
    app.keyboard.press("Escape")
    app.wait_for_timeout(1600)
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress")


def test_shift_only_shows_while_pages_pair(app, app_server):
    app.set_viewport_size({"width": 820, "height": 1180})              # portrait: one page at a time
    _alpha(app)
    app.locator('.issue-tile[data-num="1"]').click()
    expect(app.locator("#reader .rd-page").first).to_be_visible()
    app.evaluate("_rdToggleChrome(true)")
    expect(app.locator("#reader").get_by_role("button", name="Issue details")).to_be_visible()
    expect(app.locator("#reader").get_by_role("button", name="Shift pairing by one")).to_have_count(0)
    app.keyboard.press("Escape")
    app.wait_for_timeout(1600)
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress")


def test_a_long_credit_list_shows_writers_and_artists_first(app):
    import json
    credits = ([{"role": "Writer", "name": f"Writer {i}"} for i in range(8)]
               + [{"role": "Penciller", "name": "Pencils One"}]
               + [{"role": "Letterer", "name": f"Letters {i}"} for i in range(11)])          # 20 lines
    app.route("**/api/series/*/issues/1/locg-details", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"desc": "", "credits": credits})))
    _alpha(app)
    app.locator('.issue-tile[data-num="1"] .od-menu').click()
    box = app.locator("#modal .issue-modal-credits")
    expect(box.get_by_text("Writer 5")).to_be_visible()
    expect(box.get_by_text("Writer 6")).to_be_hidden()                     # six a row
    expect(box.get_by_text("Pencils One")).to_be_visible()
    expect(box.get_by_text("Letters 0")).to_be_hidden()                    # not a writer or artist
    more = box.get_by_role("button", name="+ 13 more credits")               # 2 writers + 11 letterers
    expect(more).to_be_visible()
    box.get_by_role("button", name="+2", exact=True).click()                 # the writers' row opens
    expect(box.get_by_text("Writer 7")).to_be_visible()
    expect(box.get_by_role("button", name="+ 11 more credits")).to_be_visible()
    box.get_by_role("button", name="+ 11 more credits").click()
    expect(box.get_by_text("Letters 10")).to_be_visible()
    expect(box.locator(".credit-more")).to_have_count(0)


def test_the_series_page_repaints_when_the_shelf_moves(app, app_server):
    sid = _alpha(app)
    expect(app.locator('.issue-tile[data-num="3"] .issue-tile-img.missing')).to_have_count(1)
    app.evaluate("_liveTick()")                                          # the stamp, as it stands
    with sqlite3.connect(app_server["db_path"]) as c:                     # #3 lands, behind the page's back
        c.execute("UPDATE issue_status SET owned = 1 WHERE tracked_series_id = ? AND number = 3", (sid,))
    try:
        app.evaluate("_liveTick()")                                      # it moved: a quiet repaint
        expect(app.locator('.issue-tile[data-num="3"] .issue-tile-img.missing')).to_have_count(0)
        expect(app.locator('.issue-tile[data-num="3"] .od-menu')).to_have_count(1)   # owned now: the ⋯
    finally:
        with sqlite3.connect(app_server["db_path"]) as c:
            c.execute("UPDATE issue_status SET owned = 0 WHERE tracked_series_id = ? AND number = 3", (sid,))
