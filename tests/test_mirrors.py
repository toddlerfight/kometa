"""GetComics mirrors: Pixeldrain and MediaFire behind the fs2 wall."""
from bs4 import BeautifulSoup

import kometa.getcomics_client as gc


def _client():
    c = gc.GetComicsClient.__new__(gc.GetComicsClient)
    c._mirrors = {}
    return c


def test_remember_mirrors_keeps_pixeldrain_then_mediafire():
    html = """<div><a href="https://getcomics.org/dls/main">Download Now</a>
      <a href="https://getcomics.org/dls/tb">TERABOX</a><a href="https://getcomics.org/dls/mf">MEDIAFIRE</a>
      <a href="https://getcomics.org/dls/pd">PIXELDRAIN</a></div>"""
    c = _client()
    c._remember_mirrors("https://getcomics.org/dls/main", BeautifulSoup(html, "lxml"))
    assert c._mirrors["https://getcomics.org/dls/main"] == ["https://getcomics.org/dls/pd", "https://getcomics.org/dls/mf"]


def test_mediafire_direct_link_is_read_off_the_download_button(monkeypatch):
    class R:
        status_code = 200
        text = '<html><a id="downloadButton" href="https://download2267.mediafire.com/abc/yule.cbr">Download</a></html>'
        def raise_for_status(self): pass
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: R())
    c = _client()
    assert c._mediafire_direct("https://www.mediafire.com/file/xyz/yule.cbr/file") == "https://download2267.mediafire.com/abc/yule.cbr"


def test_mirror_urls_resolves_both_kinds(monkeypatch):
    c = _client()
    c._mirrors["main"] = ["https://getcomics.org/dls/pd", "https://getcomics.org/dls/mf"]
    hops = {"https://getcomics.org/dls/pd": "https://pixeldrain.com/u/AbC123",
            "https://getcomics.org/dls/mf": "https://www.mediafire.com/file/xyz/yule.cbr/file"}
    class H:
        def __init__(self, loc): self.headers = {"location": loc}
    monkeypatch.setattr(c, "_get", lambda url, **k: H(hops[url]))
    monkeypatch.setattr(c, "_mediafire_direct", lambda url: "https://download1.mediafire.com/q/yule.cbr")
    assert c.mirror_urls("main") == ["https://pixeldrain.com/api/file/AbC123?download", "https://download1.mediafire.com/q/yule.cbr"]
