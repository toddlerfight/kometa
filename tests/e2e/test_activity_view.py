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
