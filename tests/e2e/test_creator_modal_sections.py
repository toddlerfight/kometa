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


def test_a_series_in_the_creator_window_opens_it_and_the_window_closes(app):
    alpha = app.evaluate("async () => (await (await fetch('/api/series')).json()).find(s => s.title === 'Test Comic Alpha').id")
    page = dict(PAGE, sections=[{"role": "artist", "label": "Artist", "items": [
        {"kind": "owned", "series_id": alpha, "title": "Test Comic Alpha", "owned": 2, "total": 5, "why": []}]}])
    app.route("**/api/creators/558*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(page)))
    app.wait_for_selector(".series-card")
    app.evaluate("showCreatorModal(558, 'Javier Rodríguez')")
    card = app.locator("#modal .creator-modal .series-card", has_text="Test Comic Alpha")
    expect(card).to_be_visible()
    card.click()
    expect(app.locator("#modal")).to_be_hidden()                       # the window goes…
    expect(app.locator(".issue-tile").first).to_be_visible()             # …and the series is there
    assert app.evaluate("currentView") == "series-detail" and app.evaluate("currentParams.id") == alpha


def test_the_font_comes_from_kometa_not_google(app_server, page):
    asked = []
    page.on("request", lambda r: asked.append(r.url))
    page.goto(f"{app_server['base']}/")
    page.wait_for_selector(".series-card")
    assert page.evaluate("document.fonts.ready.then(() => document.fonts.check('14px \"Victor Mono\"'))")
    assert not [u for u in asked if "googleapis" in u or "gstatic" in u], asked
    assert any("/fonts/victor-mono-latin.woff2" in u for u in asked)


def test_leaving_from_the_window_goes_dark_then_the_window_then_the_new_page(motion_app):
    """The three beats, in order: the page behind goes dark (the ghost fades), THEN the window
    sinks out, and only after it's gone does the new series rise."""
    app = motion_app
    alpha = app.evaluate("async () => (await (await fetch('/api/series')).json()).find(s => s.title === 'Test Comic Alpha').id")
    page = dict(PAGE, sections=[{"role": "artist", "label": "Artist", "items": [
        {"kind": "owned", "series_id": alpha, "title": "Test Comic Alpha", "owned": 2, "total": 5, "why": []}]}])
    app.route("**/api/creators/558*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(page)))
    app.wait_for_selector(".series-card")
    app.evaluate("showCreatorModal(558, 'Javier Rodríguez')")
    card = app.locator("#modal .creator-modal .series-card", has_text="Test Comic Alpha")
    expect(card).to_be_visible()
    app.wait_for_timeout(400)                                            # the window's own way in is over
    app.evaluate("""() => {
      window.__beats = {};
      const mark = k => { if (!(k in window.__beats)) window.__beats[k] = performance.now(); };
      const content = document.getElementById('content'), scrim = document.getElementById('modal-backdrop');
      new MutationObserver(() => {
        if (content.querySelector(':scope > .vt-ghost')) mark('dark');
        if (content.classList.contains('vt-in')) mark('in');
      }).observe(content, { childList: true, attributes: true, attributeFilter: ['class'] });
      new MutationObserver(() => {
        if (scrim.classList.contains('closing')) mark('out');
        if (scrim.classList.contains('hidden')) mark('gone');
      }).observe(scrim, { attributes: true, attributeFilter: ['class'] });
    }""")
    card.click()
    expect(app.locator("#modal")).to_be_hidden()
    expect(app.locator(".issue-tile").first).to_be_visible()
    app.wait_for_timeout(500)
    b = app.evaluate("window.__beats")
    assert set(b) >= {"dark", "out", "gone", "in"}, b
    assert b["out"] - b["dark"] >= 120, b                                # dark first…
    assert b["in"] >= b["gone"] - 5, b                                   # …the new page only once the window's gone
