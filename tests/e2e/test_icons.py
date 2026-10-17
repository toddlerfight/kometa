"""Drawn symbols (reader.js ico): the glyphs Victor Mono's Google Fonts slices lack —
✕ ✓ ♥ ♡ ★ ◆ ◇ → ← ⛶ ● — are drawn, not typed. 2026-10-17: each browser borrowed them
from its own fallback font at its own size and height, as it had the ⋯ dots."""
import sqlite3

from playwright.sync_api import expect

GLYPHS = "✕✓♥♡★◆◇→←⛶●✗▸"
PINNED: tuple = ()                    # the sort button's star and Want to read's tick were drawn last (2026-10-17)

STRAY = """(pinned) => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const el = n.parentElement;
    if (!el || el.closest('script,style') || pinned.some(p => el.closest(p))) continue;
    if ([...n.textContent].some(c => %r.includes(c)) && el.getClientRects().length) out.push(el.outerHTML.slice(0, 120));
  }
  return out;
}""" % GLYPHS

# where an icon's middle sits against its text's capitals: the same font box the text's
# own capitals sit in (canvas metrics of the computed font)
CENTRE = """(ico) => {
  const host = ico.parentElement, cs = getComputedStyle(host);
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const m = ctx.measureText('H');
  const text = [...host.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
  const r = document.createRange(); r.selectNodeContents(text);
  const box = r.getClientRects()[0];
  const baseline = box.top + m.fontBoundingBoxAscent + (box.height - m.fontBoundingBoxAscent - m.fontBoundingBoxDescent) / 2;
  const cap = baseline - m.actualBoundingBoxAscent / 2;
  const i = ico.getBoundingClientRect();
  return (i.top + i.height / 2) - cap;
}"""

ALONE = """(ico) => {
  const i = ico.getBoundingClientRect(), b = ico.parentElement.getBoundingClientRect();
  return [(i.left + i.width / 2) - (b.left + b.width / 2), (i.top + i.height / 2) - (b.top + b.height / 2)];
}"""


def _stray(app):
    return app.evaluate(STRAY, list(PINNED))


def test_no_borrowed_glyphs_and_the_chip_icons_sit_on_the_text(app):
    app.wait_for_selector(".series-card")
    for key in ("favourites", "rated"):
        ico = app.locator(f'.browse-filter-tab[data-key="{key}"] .ico')
        expect(ico).to_have_count(1)
        off = ico.evaluate(CENTRE)
        assert abs(off) <= 1, (key, off)
    assert _stray(app) == []
    app.locator(".series-card", has_text="Test Comic Alpha").click()
    expect(app.locator(".issue-tile").first).to_be_visible()
    assert _stray(app) == []
    app.locator('.issue-tile[data-num="1"] .od-menu').click()      # an owned issue's card: its ⋯
    expect(app.locator("#modal")).to_be_visible()
    for sel in (".rating-dot .ico", ".mark-heart .ico"):
        if app.locator(sel).count():
            dx, dy = app.locator(sel).first.evaluate(ALONE)
            assert abs(dx) <= 1 and abs(dy) <= 1, (sel, dx, dy)
    assert _stray(app) == []


def test_the_on_deck_x_is_centred_in_its_circle(app, app_server):
    with sqlite3.connect(app_server["db_path"]) as c:
        book_id = c.execute("SELECT id FROM books WHERE path LIKE '%Test Comic Alpha #001.cbz'").fetchone()[0]
    app.evaluate(f"async () => (await fetch('/api/books/{book_id}')).json()")
    app.evaluate(f"""async () => (await fetch('/api/books/{book_id}/progress', {{method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{page: 3, updated_at: new Date().toISOString().replace(/\\.\\d{{3}}Z$/, 'Z')}})}})).status""")
    app.evaluate("navigate('ondeck')")
    row = app.locator(".od-row", has=app.locator(".od-head", has_text="Continue reading"))
    card = row.locator(f"#od-{book_id}")
    expect(card).to_be_visible()
    card.hover()
    dx, dy = card.locator(".od-x .ico").evaluate(ALONE)
    assert abs(dx) <= .5 and abs(dy) <= .5, (dx, dy)
    assert _stray(app) == []
