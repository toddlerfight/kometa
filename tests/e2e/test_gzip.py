"""Text goes out gzipped, images don't, and versioned assets cache for a year
(kometa/main.py). Pins the Starlette exclusion-list hook: if a Starlette upgrade
stops reading DEFAULT_EXCLUDED_CONTENT_TYPES, the page test here fails."""
import json
import urllib.request


def _get(url):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.headers, r.read()


def test_app_js_is_gzipped_and_cached_for_a_year(app_server):
    h, _ = _get(f"{app_server['base']}/app.js?v=1")
    assert h.get("Content-Encoding") == "gzip"
    assert "immutable" in (h.get("Cache-Control") or "")
    h, _ = _get(f"{app_server['base']}/")
    assert "immutable" not in (h.get("Cache-Control") or "")          # index.html names the versions: never pinned


def test_a_comic_page_is_not_gzipped_again(app_server):
    base, alpha = app_server["base"], app_server["ids"]["alpha"]
    h, body = _get(f"{base}/api/series/{alpha}/issues/1/book")
    book = json.loads(__import__("gzip").decompress(body) if h.get("Content-Encoding") == "gzip" else body)
    h, _ = _get(f"{base}/api/books/{book['id']}/pages/1?w=240")
    assert (h.get("Content-Type") or "").startswith("image/")
    assert h.get("Content-Encoding") != "gzip"
