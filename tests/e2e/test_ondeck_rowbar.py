"""On Deck's row bar (app.js _paintOdBar): a stop per row under the title; a tap
scrolls that row to the top and lights its stop."""
import re

from playwright.sync_api import expect


def test_the_row_bar_jumps_to_a_row_and_lights_it(app):
    app.set_viewport_size({"width": 1180, "height": 600})            # short enough that the page scrolls
    app.locator('.nav-item[data-view="ondeck"]').click()
    stops = app.locator("#topbar-sub .od-rowbar .od-stop")
    expect(stops.first).to_have_text("Continue")
    labels = stops.all_text_contents()
    # really there to see: the first build reused .od-bar (a card's 2 px progress line) and clipped every stop
    bar = app.locator("#topbar-sub .od-rowbar").bounding_box()
    assert bar and bar["height"] >= 20, bar
    assert labels[:2] == ["Continue", "Next"] and "Added" in labels
    target = labels.index("Added")
    stops.nth(target).click()
    app.wait_for_timeout(700)                                         # smooth scroll (reduced motion: instant)
    gap = app.evaluate("""() => {
      const row = [...document.querySelectorAll('#app .od-row')].find(r =>
        r.querySelector('.od-head .series-card-title')?.textContent.trim() === 'Recently added');
      const app = document.getElementById('app');
      const atBottom = app.scrollTop + app.clientHeight >= app.scrollHeight - 4;
      return { top: row.getBoundingClientRect().top - app.getBoundingClientRect().top, atBottom };
    }""")
    assert gap["atBottom"] or 0 <= gap["top"] <= 60, gap
    expect(stops.nth(target)).to_have_class(re.compile(r"\bactive\b"))
    # leaving On Deck takes the bar with it
    app.locator('.nav-item[data-view="activity"]').click()
    expect(app.locator("#topbar-sub .od-rowbar")).to_have_count(0)
