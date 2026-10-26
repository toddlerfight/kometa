"""The web reader's strip (YondeB's, 2026-10-24/26): no slider — the strip is dragged through a
fixed box and the pages follow it; SERIES / LIST swaps it for the issues around this one;
the back is the house chevron; and leaving saves your place BEFORE the view behind reloads."""


def _open(app):
    app.set_viewport_size({"width": 1180, "height": 820})          # landscape: two-up, spreads [1] [2,3] [4] [5,6]
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    app.locator('.issue-tile[data-num="1"]').click()
    app.wait_for_function("document.querySelector('#reader #rd-stage .rd-page') && document.querySelector('#reader #rd-stage .rd-page').naturalWidth > 0")


def test_no_slider_a_box_and_the_house_chevron(app, app_server):
    _open(app)
    app.evaluate("_rdToggleChrome(true)")
    app.wait_for_timeout(300)
    assert app.locator('#reader input[type="range"]').count() == 0
    assert app.locator("#rd-box").is_visible()
    back = app.locator("#reader .rd-back")
    assert back.locator("svg").count() == 1 and back.inner_text().strip() == ""
    assert app.locator("#rd-strip-label").inner_text() == "page 1 of 6"


def test_the_pages_follow_the_strip_and_settle_where_it_stops(app, app_server):
    _open(app)
    app.evaluate("_rdToggleChrome(true)")
    app.wait_for_timeout(600)                                       # past the open's own centring
    box = app.locator("#rd-strip").bounding_box()
    app.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    app.evaluate("window._live = []; const live = _rdLive; _rdLive = i => { window._live.push(i); live(i); }")
    app.mouse.wheel(0, 4 * 52)                                      # a mouse wheel turns the strip sideways, four thumbnails
    app.wait_for_timeout(700)                                       # it's stopped: settled
    assert app.evaluate("window._live.length") > 0, "the pages didn't follow the strip"
    at = app.evaluate("_rd.spreads[_rd.at]")
    assert at[0] >= 4, at
    alts = app.evaluate("[...document.querySelectorAll('#rd-stage .rd-page')].map(i => i.alt)")
    assert alts == [f"Page {p}" for p in at]
    assert app.evaluate("[...document.querySelectorAll('#rd-strip .rd-thumb.on')].map(t => +t.dataset.page)") == at
    assert app.evaluate("document.getElementById('rd-strip').dataset.bound") == "1", "the strip was rebuilt under the finger"


def test_a_click_still_jumps(app, app_server):
    _open(app)
    app.evaluate("_rdToggleChrome(true)")
    app.wait_for_timeout(500)
    app.locator('#rd-strip .rd-thumb[data-page="4"]').click()
    app.wait_for_timeout(300)
    assert app.evaluate("_rd.spreads[_rd.at]") == [4]


def test_series_swaps_the_strip_for_the_run(app, app_server):
    _open(app)
    app.evaluate("_rdToggleChrome(true)")
    app.locator("#reader .rd-btn", has_text="SERIES").click()
    app.wait_for_selector("#rd-issues .rd-ithumb")
    on = app.locator("#rd-issues .rd-ithumb.on")
    assert on.count() == 1 and "#1" in on.inner_text() and on.is_disabled()
    gone = app.locator("#rd-issues .rd-ithumb.gone")
    assert gone.count() >= 1 and gone.first.is_disabled()          # not on the shelf: dimmed, dead to the click
    app.locator("#reader .rd-btn", has_text="SERIES").click()
    app.wait_for_selector("#rd-strip .rd-thumb")                    # and back to the pages


def test_leaving_saves_your_place_before_the_view_behind_reloads(app, app_server):
    _open(app)
    seen = []
    app.on("request", lambda r: seen.append((r.method, r.url)))
    app.keyboard.press("ArrowRight")                                # pages 2–3: a save is queued (1.2 s)
    app.wait_for_timeout(200)
    app.keyboard.press("Escape")                                    # leave at once
    app.wait_for_timeout(1500)
    put = next(i for i, (m, u) in enumerate(seen) if m == "PUT" and "/progress" in u)
    # the view behind asking the server again (page and cover images aren't that)
    first_get = next(i for i, (m, u) in enumerate(seen) if m == "GET" and "/api/" in u and "/pages/" not in u and "/cover" not in u
                     and "thumbnail" not in u)
    assert put < first_get, seen[:6]


def test_want_to_read_is_pinned_on_lists_with_no_remove_or_get(app, app_server):
    sid = app_server["ids"]["alpha"]
    app.evaluate(f"fetch('/api/wantread', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{series_id: {sid}, number: 1}})}})")
    app.wait_for_timeout(300)
    app.evaluate("navigate('readlists')")
    card = app.locator(".series-card", has_text="Want to read")
    card.wait_for()
    assert "1 TO READ" in card.inner_text()
    card.click()
    app.wait_for_function("document.getElementById('topbar-title').textContent === 'Want to read'")
    actions = app.locator("#topbar-actions").inner_text()
    assert "Remove" not in actions and "Get missing" not in actions and "Share" not in actions, actions
    app.evaluate(f"fetch('/api/wantread').then(r => r.json()).then(ws => ws.forEach(w => fetch('/api/wantread/' + w.id, {{method: 'DELETE'}})))")


def test_add_a_list_searches_the_community(app, app_server):
    app.evaluate("navigate('readlists')")
    app.locator("#topbar-actions button", has_text="Add a list").click()
    q = app.locator("#rl-q")
    q.wait_for()
    q.fill("hellboy")
    app.wait_for_selector("#rl-results .rl-result, #rl-results .rl-result-none")   # offline here: the gentle line, not a broken box
