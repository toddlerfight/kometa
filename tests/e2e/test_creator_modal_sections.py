"""The creator modal is sectioned by role (related.creator_sections): Artist, Colorist, Covers…,
each with its count; a card names only the roles its section doesn't."""
import json

from playwright.sync_api import expect

PAGE = {"creator_id": 558, "name": "Javier Rodríguez", "pending": False,
        "shelf": [{"kind": "owned", "series_id": 1, "title": "Absolute Martian Manhunter", "owned": 12, "total": 12, "why": ["Artist: Javier Rodríguez"]}],
        "catalogue": [{"kind": "catalogue", "metron_series_id": 9, "title": "Daredevil", "year": 2011, "cover": "/x.jpg", "owned": 0, "total": 9, "why": ["Colorist: Javier Rodríguez"]}],
        "sections": [
            {"role": "artist", "label": "Artist", "items": [{"kind": "owned", "series_id": 1, "title": "Absolute Martian Manhunter", "owned": 12, "total": 12, "why": ["Also colorist, cover"]}]},
            {"role": "colorist", "label": "Colorist", "items": [{"kind": "catalogue", "metron_series_id": 9, "title": "Daredevil", "year": 2011, "cover": "/x.jpg", "owned": 0, "total": 9, "why": []}]}]}


def test_the_creator_modal_shows_one_section_per_role(app):
    app.route("**/api/creators/558*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(PAGE)))
    app.wait_for_selector(".series-card")
    app.evaluate("showCreatorModal(558, 'Javier Rodríguez')")
    modal = app.locator("#modal .creator-modal")
    expect(modal.locator(".od-head")).to_have_count(2)
    assert [t.strip() for t in modal.locator(".od-head .series-card-title").all_inner_texts()] == ["Artist", "Colorist"]
    expect(modal.locator(".series-card").first).to_contain_text("Also colorist, cover")
    expect(modal).not_to_contain_text("On the shelf")
