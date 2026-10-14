"""On Deck: the ✕ on a Continue reading card is Mark as read; Undo puts the place back."""
import sqlite3

from playwright.sync_api import expect


def _alpha_1(app_server) -> int:
    with sqlite3.connect(app_server["db_path"]) as c:
        return c.execute("SELECT id FROM books WHERE path LIKE '%Test Comic Alpha #001.cbz'").fetchone()[0]


def _progress(app, book_id):
    # awaited in the page: a bare fetch() in wait_for_function is a Promise, and a Promise is always truthy
    return app.evaluate(f"async () => (await (await fetch('/api/books/{book_id}')).json()).progress")


def test_the_x_marks_it_read_and_undo_puts_the_place_back(app, app_server):
    book_id = _alpha_1(app_server)
    _progress(app, book_id)                          # open it first, as the reader does: that's what fills its page count
    status = app.evaluate(f"""async () => (await fetch('/api/books/{book_id}/progress', {{method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{page: 3, updated_at: new Date().toISOString().replace(/\\.\\d{{3}}Z$/, 'Z')}})}})).status""")
    assert status == 200
    app.evaluate("navigate('ondeck')")
    row = app.locator(".od-row", has=app.locator(".od-head", has_text="Continue reading"))
    card = row.locator(f"#od-{book_id}")
    expect(card).to_be_visible()
    card.hover()
    card.locator(".od-x").click()
    expect(card).to_have_count(0)                                       # gone from Continue reading
    p = _progress(app, book_id)
    assert p["completed"] and p["page"] == 6
    app.locator("#toast .toast-action", has_text="Undo").click()
    app.wait_for_timeout(500)
    p = _progress(app, book_id)
    assert not p["completed"] and p["page"] == 3                        # your place, not page 1
    expect(row.locator(f"#od-{book_id}")).to_be_visible()
    with sqlite3.connect(app_server["db_path"]) as c:
        c.execute("DELETE FROM read_progress WHERE book_id = ?", (book_id,))
