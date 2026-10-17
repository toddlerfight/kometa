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
