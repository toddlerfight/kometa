"""Library by release date (app.js renderBrowse): a stack wears the date it sorts by,
and series with the same date or none are in title order, leading punctuation and
'The' ignored. 2026-10-17: the Batman stack sat among the Nov 4 series with no date
on it, and 'Namwolf came before 100 Bullets."""
import json

from playwright.sync_api import expect


def _canned_series(page, base, rows):
    page.route("**/api/series", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(rows)))
    page.route("**/api/shelf", lambda r: r.fulfill(status=200, content_type="application/json", body='{"series": [], "tracked_reading": {}}'))
    page.goto(f"{base}/#library")


def _row(i, title, **kw):
    base = {"id": i, "title": title, "publisher": "Dark Horse Comics", "owned": 4, "missing": 0, "upcoming": 0, "calendar_date": None,
            "on_pull_list": False, "favourite": False, "family_parent": None, "family_children": [], "metron_type": "Limited Series",
            "match_status": "auto", "franchise": None, "newest_file_at": None}
    base.update(kw)
    return base


def test_ties_and_undated_go_by_title_and_a_stack_shows_its_date(app_server, page):
    fr = {"key": "batman", "name": "Batman", "count": 2}
    rows = [_row(1, "Zed", calendar_date="2099-11-04"), _row(2, "The Apple", calendar_date="2099-11-04"),
            _row(3, "Batman: Part 1", franchise=fr, calendar_date="2099-11-11"),
            _row(4, "Batman: Part 2", franchise=fr, calendar_date="2099-11-04"),
            _row(5, "Wytches"), _row(6, "'Namwolf"), _row(7, "100 Bullets"), _row(8, "Mid", calendar_date="2099-11-05")]
    _canned_series(page, app_server["base"], rows)
    page.evaluate("localStorage.removeItem('kometa.librarySort')")
    page.reload()
    expect(page.locator(".series-card")).to_have_count(7)
    titles = page.locator(".series-card > .series-card-footer .series-card-title").all_inner_texts()
    assert titles == ["The Apple", "Batman", "Zed", "Mid", "100 Bullets", "'Namwolf", "Wytches"], titles
    stack = page.locator(".stack-card")
    expect(stack.locator(".series-card-next-release")).to_have_text("Nov 4")       # its soonest member's date
    # the date sits on the front cover, above the fan, like a single card's
    pill = stack.locator(".series-card-next-release").bounding_box()
    front = stack.locator(".stack-cover-0").bounding_box()
    assert front["y"] < pill["y"] and pill["y"] + pill["height"] <= front["y"] + front["height"] + 1, (pill, front)
    on_top = page.evaluate("""() => { const pill = document.querySelector('.stack-card .series-card-next-release');
      pill.style.pointerEvents = 'auto';                       // it ignores the pointer, so the probe would see through it
      const p = pill.getBoundingClientRect();
      const el = document.elementFromPoint(p.x + p.width / 2, p.y + p.height / 2); return el.className; }""")
    assert on_top == 'series-card-next-release', on_top
    page.locator("#sort-alpha").click()
    titles = page.locator(".series-card > .series-card-footer .series-card-title").all_inner_texts()
    assert titles.index("'Namwolf") == titles.index("Mid") + 1, titles          # with the Ns, not up front
