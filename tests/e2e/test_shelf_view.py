"""Every series is a Kometa series (docs/reader-spec.md, 2026-10-08): a folder
nobody added becomes a normal series with the pull list OFF, waiting to be
matched — and its files are readable straight away."""
import json
import os
import shutil
import time
import urllib.request

from playwright.sync_api import expect


def _post(base, path):
    urllib.request.urlopen(urllib.request.Request(f"{base}{path}", method="POST"))


def _series_id(base, title):
    for s in json.load(urllib.request.urlopen(f"{base}/api/series")):
        if s["title"] == title:
            return s["id"]


def test_new_folder_becomes_a_series_pull_off_and_readable(app, app_server):
    import io, zipfile
    from PIL import Image
    base = app_server["base"]
    root = os.path.join(os.path.dirname(app_server["db_path"]), "comics")
    folder = os.path.join(root, "Indie House", "Shelf Only")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(os.path.join(folder, "Shelf Only #001.cbz"), "w") as z:
        for i in range(1, 4):
            buf = io.BytesIO()
            Image.new("RGB", (600, 900), (90, 40 * i, 40)).save(buf, "JPEG")
            z.writestr(f"p{i}.jpg", buf.getvalue())
    sid = None
    try:
        _post(base, "/api/shelf/scan")
        for _ in range(40):
            sid = _series_id(base, "Shelf Only")
            if sid:
                break
            time.sleep(0.25)
        assert sid, "the folder never became a series"
        app.reload()
        card = app.locator(".series-card", has_text="Shelf Only")
        expect(card).to_be_visible()
        app.locator(".browse-filter-tab", has_text="Pull list").click()
        expect(card).to_have_count(0)                             # added, not pulled
        app.locator(".browse-filter-tab", has_text="Pull list").click()
        app.locator(".browse-filter-tab", has_text="Needs match").click()
        expect(card).to_be_visible()                              # matcher found nothing
        app.locator(".browse-filter-tab", has_text="Needs match").click()

        card.click()
        expect(app.locator("#match-banner")).to_contain_text("Pick the run")
        expect(app.locator(".pull-switch input")).not_to_be_checked()
        app.locator('.issue-tile[data-num="1"]').click()           # owned, straight from disk
        app.locator("#modal").get_by_role("button", name="Read").click()
        expect(app.locator("#reader .rd-page")).to_have_attribute("alt", "Page 1")
        app.keyboard.press("Escape")
    finally:
        if sid:
            urllib.request.urlopen(urllib.request.Request(f"{base}/api/series/{sid}", method="DELETE"))
        shutil.rmtree(os.path.join(root, "Indie House"), ignore_errors=True)
        _post(base, "/api/shelf/scan")
        time.sleep(1)
