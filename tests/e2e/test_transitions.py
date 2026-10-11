"""View transitions + the loading bar (app.js _navBegin/_vtLeave/_vtReveal,
reader.js _rdShowLoading/closeReader). The `app` fixture runs reduced-motion
(instant swaps, bar still shows); `motion_app` turns motion on."""
import os
import re
import sqlite3
import time

import pytest
from playwright.sync_api import expect

# Records what the transition machinery did, from inside the page: every class
# #content wore, every ghost mounted, whether the bar ever lit up.
RECORDER = """() => {
  (window.__vtObs || []).forEach(o => o.disconnect());     // a re-arm must not double-count
  const rec = window.__vt = { content: [], ghosts: 0, bar: false };
  const c = document.getElementById('content'), b = document.getElementById('nav-bar');
  const oc = new MutationObserver(ms => { for (const m of ms) {
    if (m.type === 'attributes') rec.content.push(c.className);
    else for (const n of m.addedNodes) if (n.classList && n.classList.contains('vt-ghost')) rec.ghosts++;
  } });
  oc.observe(c, { attributes: true, attributeFilter: ['class'], childList: true });
  const ob = new MutationObserver(() => { if (b.classList.contains('on')) rec.bar = true; });
  ob.observe(b, { attributes: true, attributeFilter: ['class'] });
  window.__vtObs = [oc, ob];
}"""

# Samples #layout and #reader opacity every frame for `ms`.
SAMPLER = """ms => { window.__op = []; const t0 = performance.now();
  const tick = () => {
    const L = document.getElementById('layout'), R = document.getElementById('reader');
    __op.push({ t: performance.now() - t0, layout: +getComputedStyle(L).opacity,
      reader: R && !R.classList.contains('hidden') ? +getComputedStyle(R).opacity : 0 });
    if (performance.now() - t0 < ms) requestAnimationFrame(tick);
  }; requestAnimationFrame(tick); }"""


def _slow(seconds):
    def handler(route):
        time.sleep(seconds)
        route.continue_()
    return handler


def _settled(page):
    page.wait_for_function("""() => !document.querySelector('.vt-ghost')
      && !/vt-/.test(document.getElementById('content').className)""")


def test_loading_bar_shows_for_a_slow_view_and_goes(app, app_server):
    alpha = app_server["ids"]["alpha"]
    expect(app.locator(".series-card").first).to_be_visible()
    app.evaluate(RECORDER)
    app.route(re.compile(rf"/api/series/{alpha}$"), _slow(0.6))
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(app.locator('.issue-tile[data-num="1"]')).to_be_visible(timeout=5000)
    assert app.evaluate("__vt.bar"), "a 600 ms view should light the bar"
    expect(app.locator("#nav-bar")).not_to_have_class(re.compile(r"\bon\b"))
    app.unroute(re.compile(rf"/api/series/{alpha}$"))


def test_a_fast_view_never_flashes_the_bar(app):
    expect(app.locator(".series-card").first).to_be_visible()
    app.locator('.nav-item[data-view="settings"]').click()
    app.wait_for_timeout(300)
    app.evaluate(RECORDER)
    app.locator('.nav-item[data-view="library"]').click()     # painted from cache: no wait at all
    expect(app.locator(".series-card").first).to_be_visible()
    app.wait_for_timeout(250)
    assert not app.evaluate("__vt.bar")


def test_library_to_series_and_back_transitions_and_keeps_the_library(motion_app):
    page = motion_app
    expect(page.locator(".series-card").first).to_be_visible()
    _settled(page)
    page.evaluate(RECORDER)
    page.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(page.locator('.issue-tile[data-num="1"]')).to_be_visible()
    _settled(page)
    fwd = page.evaluate("__vt")
    assert fwd["ghosts"] == 1, "the library should fade off a ghost"
    assert any("vt-pending" in c for c in fwd["content"])
    assert any(re.search(r"\bvt-in\b", c) for c in fwd["content"])

    page.evaluate(RECORDER)
    page.go_back()
    expect(page.locator(".series-card", has_text="Beta Saga")).to_be_visible()
    _settled(page)
    back = page.evaluate("__vt")
    assert back["ghosts"] == 1
    assert any("vt-in-back" in c for c in back["content"]), "Back settles the other way"
    assert page.evaluate("+getComputedStyle(document.getElementById('app')).opacity") == 1
    expect(page.locator(".series-card")).to_have_count(3)                      # all of it, visible, clickable
    page.locator(".series-card", has_text="Beta Saga").click()
    expect(page).to_have_url(re.compile(r"#series-detail\?id=\d+"))


def test_reader_fades_in_over_the_series_and_back_out(motion_app, app_server):
    page = motion_app
    page.locator(".series-card", has_text="Test Comic Alpha").click()
    page.locator('.issue-tile[data-num="1"]').click()
    expect(page.locator("#modal")).to_be_visible()
    _settled(page)
    page.evaluate(SAMPLER, 900)
    page.locator("#modal").get_by_role("button", name="Read").click()
    expect(page.locator("#reader #rd-stage .rd-page")).to_be_visible(timeout=10000)
    page.wait_for_timeout(950)
    ops = page.evaluate("__op")
    assert any(0.05 < s["layout"] < 0.95 for s in ops), "the series should FADE, not vanish"
    # the reader only comes up once the series is (nearly) gone
    assert all(s["reader"] < 0.5 for s in ops if s["layout"] > 0.5)
    assert ops[-1]["layout"] == 0 and ops[-1]["reader"] == 1
    expect(page.locator("#reader")).to_have_class(re.compile(r"\brd-enter\b"))
    assert page.evaluate("document.body.classList.contains('reading')")

    page.evaluate(SAMPLER, 700)
    page.keyboard.press("Escape")
    expect(page.locator("#reader")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#series-detail\?id=\d+"))
    page.wait_for_timeout(750)
    ops = page.evaluate("__op")
    assert any(0.05 < s["layout"] < 0.95 for s in ops), "closing should fade the series back up"
    assert ops[-1]["layout"] == 1
    expect(page.locator('.issue-tile[data-num="1"]')).to_be_visible()

    # leave no trace: the open may have stamped progress on page 1
    page.wait_for_timeout(1600)
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress")


VIDEO = os.environ.get("KOMETA_TRANSITIONS_VIDEO")


@pytest.mark.skipif(not VIDEO, reason="set KOMETA_TRANSITIONS_VIDEO=/path/out.webm to record the demo clip")
def test_record_transitions_video(app_server, browser):
    """Not a check — a demo reel: Library → series → Back → reader → close."""
    import shutil
    import tempfile
    from conftest import quarantine
    alpha = app_server["ids"]["alpha"]
    vdir = tempfile.mkdtemp()
    size = {"width": 1180, "height": 820}
    ctx = browser.new_context(reduced_motion="no-preference", viewport=size,
                              record_video_dir=vdir, record_video_size=size)
    page = ctx.new_page()
    quarantine(page)
    page.route(re.compile(rf"/api/series/{alpha}$"), _slow(0.4))
    page.route("**/api/series/*/issues/1/book", _slow(0.4))
    page.route(re.compile(r"/api/books/\d+$"), _slow(0.4))
    page.goto(f"{app_server['base']}/")
    expect(page.locator(".series-card").first).to_be_visible()
    page.wait_for_timeout(1200)
    page.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(page.locator('.issue-tile[data-num="1"]')).to_be_visible()
    page.wait_for_timeout(1400)
    page.go_back()
    expect(page.locator(".series-card", has_text="Beta Saga")).to_be_visible()
    page.wait_for_timeout(1200)
    page.locator(".series-card", has_text="Test Comic Alpha").click()
    page.locator('.issue-tile[data-num="1"]').click()
    page.wait_for_timeout(900)
    page.locator("#modal").get_by_role("button", name="Read").click()
    expect(page.locator("#reader #rd-stage .rd-page")).to_be_visible(timeout=10000)
    page.wait_for_timeout(1500)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(900)
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)
    path = page.video.path()
    ctx.close()
    os.makedirs(os.path.dirname(VIDEO), exist_ok=True)
    shutil.copy(path, VIDEO)
    shutil.rmtree(vdir, ignore_errors=True)
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress")
