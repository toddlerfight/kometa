"""Whole shelf in the Library (docs/reader-spec.md step 2): an untracked folder
shows as a card, the Tracked chip hides it, its page opens books in the reader."""
import os
import shutil
import time
import urllib.request

from playwright.sync_api import expect


def _scan(base):
    urllib.request.urlopen(urllib.request.Request(f"{base}/api/shelf/scan", method="POST"))
    time.sleep(1.0)


def test_untracked_series_is_on_the_shelf_and_readable(app, app_server):
    import io, zipfile
    from PIL import Image
    root = os.path.join(os.path.dirname(app_server["db_path"]), "comics")
    folder = os.path.join(root, "Indie House", "Shelf Only")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(os.path.join(folder, "Shelf Only #001.cbz"), "w") as z:
        for i in range(1, 4):
            buf = io.BytesIO()
            Image.new("RGB", (600, 900), (90, 40 * i, 40)).save(buf, "JPEG")
            z.writestr(f"p{i}.jpg", buf.getvalue())
    try:
        _scan(app_server["base"])
        app.reload()
        card = app.locator(".series-card", has_text="Shelf Only")
        expect(card).to_be_visible()
        expect(app.locator(".series-card")).to_have_count(4)
        app.locator(".browse-filter-tab", has_text="Tracked").click()
        expect(app.locator(".series-card")).to_have_count(3)
        expect(card).to_have_count(0)
        app.locator(".browse-filter-tab", has_text="Tracked").click()

        card.click()
        expect(app.locator("#topbar-title")).to_have_text("Shelf Only")
        expect(app.locator(".issue-tile")).to_have_count(1)
        app.get_by_role("button", name="Start #1").click()
        expect(app.locator("#reader .rd-page")).to_have_attribute("alt", "Page 1")
        app.keyboard.press("Escape")
        expect(app.locator("#reader")).to_be_hidden()
    finally:
        shutil.rmtree(os.path.join(root, "Indie House"), ignore_errors=True)
        _scan(app_server["base"])
