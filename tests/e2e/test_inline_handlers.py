"""Inline handlers carry titles safely (reader.js jsArg). Security review 2026-10-11 item 5:
only '"' was escaped, so a title holding a literal &quot; decoded back into a real quote
before the handler ran — `&quot;});alert(document.domain)//` broke out."""
from playwright.sync_api import expect

EVIL = '&quot;});window.__pwned=1;//'


def test_a_title_cant_break_out_of_its_handler(app):
    app.wait_for_selector(".series-card")
    app.evaluate("""(evil) => {
      const host = document.createElement('div');
      host.id = 'xss-probe';
      host.innerHTML = _trendCard({ rank: 1, series: evil, title: evil, publisher: evil, cover: '/x.jpg' })
                     + _readerCard({ name: evil, label: evil, cover: '/x.jpg' });
      document.body.appendChild(host);
    }""", EVIL)
    find = app.locator("#xss-probe .rel-get").first
    expect(find).to_have_count(1)
    find.click()                                                  # FIND → the add wizard, searching for the title
    expect(app.locator("#modal")).to_be_visible()
    assert app.evaluate("window.__pwned") is None
    expect(app.locator("#wizard-search")).to_have_value(EVIL)           # the title arrived whole, as text
    app.keyboard.press("Escape")
    app.locator("#xss-probe .series-card").first.click()        # the card itself: the catalogue sheet
    assert app.evaluate("window.__pwned") is None
