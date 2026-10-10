"""The header search palette: '/' opens it anywhere, results drop down grouped,
arrow keys + Enter open, Esc closes, recent searches when the box is empty."""
import json

from playwright.sync_api import expect

RESULT = {
    "q": "batman 13",
    "top": {"kind": "issue", "series_id": 1, "series": "Test Comic Alpha", "number": 13, "label": "#13",
            "book_id": None, "owned": False, "cover": "/api/series/1/issues/13/thumbnail"},
    "series": [{"id": 2, "title": "Beta Saga", "publisher": "Image", "year_began": 2012, "owned": 3, "total": 3,
                "why": "Writer: Paul Pope", "cover": "/api/series/2/thumbnail"}],
    "people": [{"id": 1116, "name": "Paul Pope", "roles": ["Writer", "Artist"], "count": 4}],
    "lists": [{"id": 7, "name": "Doom list", "total": 9, "owned": 2, "cover_book_id": None, "cover": None}],
    "issues": [{"series_id": 1, "series": "Test Comic Alpha", "number": 13, "label": "#13",
                "book_id": None, "owned": False, "cover": "/api/series/1/issues/13/thumbnail"}],
}


def test_slash_opens_grouped_results_and_keys_navigate(app):
    asked = []
    app.route("**/api/search?**", lambda r: (asked.append(r.request.url),
              r.fulfill(status=200, content_type="application/json", body=json.dumps(RESULT)))[1])
    expect(app.locator(".series-card").first).to_be_visible()
    app.locator("body").click()
    app.keyboard.press("/")
    box = app.locator("#global-search")
    expect(box).to_be_focused()
    box.type("batman 13", delay=10)
    pal = app.locator("#search-pal")
    expect(pal).to_be_visible()
    expect(pal.locator(".pal-head")).to_have_text(["Top hit", "Series", "People", "Reading lists"])   # the issue IS the top hit
    expect(pal.locator(".pal-row.sel")).to_contain_text("Test Comic Alpha #13")
    expect(pal).to_contain_text("Writer: Paul Pope")
    assert asked and "q=batman%2013" in asked[-1]
    # Down twice → the person; Enter opens the creator modal (canned)
    app.route("**/api/creators/1116**", lambda r: r.fulfill(status=200, content_type="application/json",
              body=json.dumps({"name": "Paul Pope", "shelf": [], "catalogue": [], "pending": False})))
    box.press("ArrowDown"); box.press("ArrowDown")
    expect(pal.locator(".pal-row.sel")).to_contain_text("Paul Pope")
    box.press("Enter")
    expect(pal).to_be_hidden()
    expect(app.locator("#modal")).to_contain_text("Paul Pope")


def test_escape_closes_and_recent_searches_show_when_empty(app):
    app.route("**/api/search?**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(RESULT)))
    box = app.locator("#global-search")
    box.click(); box.type("batman 13", delay=10)
    expect(app.locator("#search-pal .pal-row.sel")).to_be_visible()
    box.press("Enter")                                   # opens the series page at #13 (not on the shelf)
    import re
    expect(app).to_have_url(re.compile(r"#series-detail\?id=1"))
    box.click()
    pal = app.locator("#search-pal")
    expect(pal).to_be_visible()
    expect(pal.locator(".pal-head")).to_have_text(["Recent"])
    expect(pal.locator(".pal-row")).to_contain_text(["batman 13"])
    box.press("Escape")
    expect(pal).to_be_hidden()
