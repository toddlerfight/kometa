"""An issue no catalogue has art for yet opens its card on the blank cover block —
not a dimmed broken image with its alt text showing through (100 Bullets: The US of
Anger #5, 2026-10-25)."""
from playwright.sync_api import expect


def test_an_artless_issue_card_shows_the_blank_cover(app, app_server):
    sid = app_server["ids"]["alpha"]
    app.route("**/api/series/*/issues/4/thumbnail**", lambda r: r.fulfill(status=404, body=""))
    app.evaluate(f"showIssueModal({sid}, 4)")
    cover = app.locator("#modal .issue-modal-cover")
    expect(cover.locator(".issue-modal-no-cover")).to_have_count(1)
    expect(cover.locator("img")).to_have_count(0)
