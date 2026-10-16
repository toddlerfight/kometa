"""Closing one window and opening the next in the same click must leave the next one
open: 'Find in catalogue' opened the Add search and closeModal's pending fade-out hid
it 200 ms later (2026-10-16)."""
from playwright.sync_api import expect


def test_a_window_opened_while_another_fades_out_stays_open(app, app_server):
    app.evaluate("showModal('<div class=\"modal-title\">First</div>')")
    expect(app.locator("#modal")).to_be_visible()
    app.evaluate("closeModal(); showModal('<div class=\"modal-title\" id=\"second\">Second</div>')")
    app.wait_for_timeout(400)                                   # past the 200 ms fade-out
    expect(app.locator("#second")).to_be_visible()
    expect(app.locator("#modal-backdrop")).not_to_have_class("hidden")
    app.evaluate("closeModal()")
    app.wait_for_timeout(400)
    expect(app.locator("#modal")).to_be_hidden()                # an ordinary close still closes
