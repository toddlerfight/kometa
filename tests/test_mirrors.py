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


def test_two_group_post_keeps_the_first_groups_mirrors(monkeypatch):
    """Deadpool #8 (2023), 2026-10-15: HD and SD groups, buttons as bare siblings of
    the Language line. The first group's Pixeldrain wins; the SD group stays out."""
    post = """<section class="post-contents">
      <p>Deadpool #8 (HD-Digital) Language : English</p>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/MAIN">DOWNLOAD NOW</a></div>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/PD">PIXELDRAIN</a></div>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/MF">MEDIAFIRE</a></div>
      <hr/>
      <p>Deadpool #8 (SD-Digital) Language : English</p>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/SD">Download Now</a></div>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/MF2">MEDIAFIRE</a></div>
      <hr/></section>"""

    class R:
        def __init__(self, text="", loc=""):
            self.text, self.headers, self.status_code = text, {"location": loc}, 200
        def raise_for_status(self): pass
    hops = {"https://getcomics.org/dls/PD": "https://pixeldrain.com/u/Dp8"}
    c = gc.GetComicsClient()
    monkeypatch.setattr(c, "_get", lambda url, **k: R(loc=hops[url]) if url in hops else R(text=post))
    monkeypatch.setattr(c, "_mediafire_direct", lambda url: None)
    href, _ = c._extract_download("https://getcomics.org/marvel/deadpool-8-2023/")
    assert href == "https://pixeldrain.com/api/file/Dp8?download"
    assert c._mirrors[href][0] == "https://getcomics.org/dls/MAIN"
    assert "https://getcomics.org/dls/MF2" not in c._mirrors["https://getcomics.org/dls/MAIN"]


def test_year_tagged_series_name_still_scores():
    """Deadpool (2023), 2026-10-15: usenet had the issue, scored it 0, the row went
    to GetComics and died on the fs2 wall."""
    from kometa.usenet_client import _nzb_score, _pack_score
    assert _nzb_score("Deadpool 008 (2023) (Digital) (Zone-Empire)", "Deadpool (2023)", 8.0) == 15
    assert _nzb_score("Batman 151 (2024) (Digital)", "Batman- One Dark Knight (2021-)", 151.0) == 0
    assert _nzb_score("Rumble 003 (2017) (Digital)", "Rumble (2017)", 3.0) == 15


def test_year_guard_falls_back_to_the_name_tag():
    from kometa.prowlarr_client import _drop_year_mismatches
    rs = [{"title": "Rumble 003 (2014) (Digital)"}, {"title": "Rumble 003 (2017) (Digital)"}]
    kept = _drop_year_mismatches(rs, "Rumble (2017)", None, store_date="2017-08-01")
    assert [r["title"] for r in kept] == ["Rumble 003 (2017) (Digital)"]
