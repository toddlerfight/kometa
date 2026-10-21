"""Activity's top bar (2026-10-17): one action, Sweep Missing; the view as the Library's
chips, In progress / History; Clear history at the end of History, tap twice."""
import re

from playwright.sync_api import expect


def test_activity_has_one_action_view_chips_and_clear_history_at_the_end(app):
    app.locator('.nav-item[data-view="activity"]').click()
    actions = app.locator("#topbar-actions button")
    expect(actions).to_have_count(1)
    expect(actions.first).to_have_text("Sweep Missing")
    now, hist = app.locator("#act-view-now"), app.locator("#act-history-btn")
    expect(now).to_have_class(re.compile(r"\bbrowse-filter-tab\b.*\bactive\b"))
    expect(hist).not_to_have_class(re.compile(r"\bactive\b"))
    expect(app.locator("#act-clear")).to_have_count(0)                  # no Clear history outside History
    expect(app.locator("#harvest-progress")).to_have_count(0)            # the numbers board is gone

    hist.click()
    expect(hist).to_have_class(re.compile(r"\bactive\b"))
    expect(now).not_to_have_class(re.compile(r"\bactive\b"))
    clear = app.locator("#act-clear button")
    expect(clear).to_have_text("Clear history")
    clear.click()                                                        # armed, not cleared
    expect(clear).to_have_text("Clear history — sure?")
    expect(app.locator(".act-row")).not_to_have_count(0)
    expect(clear).to_have_text("Clear history", timeout=5000)           # disarms after 3 s

    now.click()
    expect(app.locator("#act-clear")).to_have_count(0)


def test_a_running_download_has_a_stop_x_and_placing_does_not(app):
    """Before 2026-10-18 a running row had no X — the Loveless pack ran 871 MB
    with no way to stop it from Activity."""
    app.locator('.nav-item[data-view="activity"]').click()
    got = app.evaluate(
        """() => Object.fromEntries(
      ['downloading', 'pending_usenet', 'pending_torrent', 'processing', 'searching'].map(st => {
        const html = _actActionsHtml({ id: 9, state: st, kind: 'trade', issue_number: null });
        return [st, html.includes('aria-label="Stop download"')];
      }))"""
    )
    assert got == {"downloading": True, "pending_usenet": True, "pending_torrent": True,
                   "processing": False, "searching": False}


def test_an_empty_or_failed_row_says_when_it_was_tried_and_when_it_tries_next(app):
    """Not Found read the same at 3:06pm as three days later (2026-10-21)."""
    app.locator('.nav-item[data-view="activity"]').click()
    got = app.evaluate(
        """() => {
      const stamp = q => { const t = document.createElement('template');
        t.innerHTML = _actRowHtml({ id: 9, title: 'Absolute Flash', issue_number: 20, kind: 'issue',
                                    tracked_series_id: 1, ...q }).trim();
        const w = t.content.querySelector('.act-row-when'); return w ? w.textContent : null; };
      const ago = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 19).replace('T', ' ');
      const soon = new Date(Date.now() + 2 * 86400000).toISOString().slice(0, 19).replace('T', ' ');
      return {
        not_found: stamp({ state: 'not_found', updated_at: ago, next_try: soon }),
        failed: stamp({ state: 'failed', updated_at: ago, next_try: null }),
        parked: stamp({ state: 'queued', updated_at: ago, next_try: soon }),
        queued: stamp({ state: 'queued', updated_at: ago, next_try: null }),
        done: stamp({ state: 'done', updated_at: ago }),
      };
    }"""
    )
    assert re.fullmatch(r"Tried \w{3} \d+(:\d\d)?[ap]m · next \w{3} \d+(:\d\d)?[ap]m", got["not_found"])
    assert re.fullmatch(r"Tried \w{3} \d+(:\d\d)?[ap]m", got["failed"])        # only Retry tries it again
    assert re.fullmatch(r"next \w{3} \d+(:\d\d)?[ap]m", got["parked"])         # a rate-limit park
    assert got["queued"] is None and got["done"] is None
