"""Readers like you (kometa/cbr_people.py) on On Deck: hidden while nothing's
harvested, a row per seed plus the overall row once there is. Canned responses."""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

ITEM = {"series_path": "/comic-books/reviews/image-comics/monstress", "name": "Monstress", "year": 2015,
        "publisher": "image-comics", "publisher_name": "Image", "score": 1.2, "supporters": 7, "critics": 3, "users": 4,
        "because": ["Saga"], "label": "Liked by 7 readers who rate Saga highly"}


def _canned(page, rows, items):
    page.route("**/api/discover/community/because-ondeck*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"rows": rows})))
    page.route("**/api/discover/community?*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"items": items, "seeds": 1, "people": 1})))


def test_no_harvest_no_rows(app_server, page):
    _canned(page, [], [])
    page.goto(f"{app_server['base']}/#ondeck")
    expect(page.locator("#od-readers")).to_be_attached()
    page.wait_for_timeout(500)
    assert page.locator(".readers-row").count() == 0


def test_rows_appear_and_open_the_catalogue_sheet(app_server, page):
    seed = {"seed": {"series_id": 1, "title": "Saga", "supporters": 5}, "items": [ITEM]}
    _canned(page, [seed], [dict(ITEM, name=f"Run {i}") for i in range(4)])
    page.goto(f"{app_server['base']}/#ondeck")
    expect(page.get_by_text("Because you're reading Saga")).to_be_visible()
    page.locator("#od-readers .series-card").first.click()
    expect(page.locator(".issue-modal-num")).to_have_text("Monstress")
    expect(page.get_by_text("Liked by 7 readers who rate Saga highly")).to_be_visible()
