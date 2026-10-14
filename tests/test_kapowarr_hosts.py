"""Hosts and walls borrowed from Kapowarr's playbook (2026-10-15): FlareSolverr
for Cloudflare, Pixeldrain folders, MediaFire's hidden links, every
mirror in a group, and a cleaner GetComics search."""
import base64
import json
import struct

import pytest
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import kometa.downloader as dl
import kometa.flaresolverr as fs
import kometa.getcomics_client as gc


class Resp:
    def __init__(self, status=200, headers=None, body=b"", url="https://host/x"):
        self.status_code, self.headers, self._body, self.url = status, requests.structures.CaseInsensitiveDict(headers or {}), body, url
        self.text = body.decode("latin-1") if isinstance(body, bytes) else body
        self.closed = False
    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), 7):
            yield self._body[i:i + 7]
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")
    def close(self):
        self.closed = True
    def json(self):
        return json.loads(self.text)


# --- FlareSolverr ---------------------------------------------------------------

def test_cloudflare_wall_is_cleared_and_the_file_fetched(monkeypatch):
    seen = []
    def get(url, headers=None, **k):
        seen.append(headers.get("Cookie"))
        if headers.get("Cookie") == "cf_clearance=ok":
            return Resp(200, {"content-length": "3"}, b"Rar", url)
        return Resp(403, {"cf-mitigated": "challenge", "server": "cloudflare"}, b"", url)
    monkeypatch.setattr(dl.requests, "get", get)
    monkeypatch.setattr(dl, "_unwrap_hosted", lambda u: u)
    monkeypatch.setattr(fs, "_base", lambda: "http://nas:8191")
    monkeypatch.setattr(fs, "clearance", lambda url: ("UA-FS", {"cf_clearance": "ok"}))
    r = dl._get_with_retries("https://fs2.comicfiles.ru/a.cbr", "Download")
    assert r.status_code == 200 and seen == [None, "cf_clearance=ok"]


def test_a_plain_403_is_not_a_wall():
    assert not fs.is_challenge(Resp(403, {"server": "nginx"}))
    assert fs.is_challenge(Resp(403, {"server": "cloudflare"}))


def test_clearance_is_solved_once_per_domain(monkeypatch):
    fs._cache.clear()
    calls = []
    monkeypatch.setattr(fs, "_base", lambda: "http://nas:8191")
    def post(url, json=None, timeout=None):
        calls.append(json["url"])
        return Resp(200, body=b'{"status":"ok","solution":{"userAgent":"UA","cookies":[{"name":"cf_clearance","value":"v","expiry":9999999999}]}}')
    monkeypatch.setattr(fs.requests, "post", post)
    assert fs.clearance("https://fs2.comicfiles.ru/a/b.cbr") == ("UA", {"cf_clearance": "v"})
    assert fs.clearance("https://fs2.comicfiles.ru/c.cbr") == ("UA", {"cf_clearance": "v"})
    assert calls == ["https://fs2.comicfiles.ru/"]
    fs._cache.clear()


def test_no_solver_configured_means_the_403_stands(monkeypatch):
    monkeypatch.setattr(dl.requests, "get", lambda url, **k: Resp(403, {"cf-mitigated": "challenge"}, b"", url))
    monkeypatch.setattr(dl, "_unwrap_hosted", lambda u: u)
    monkeypatch.setattr(fs, "_base", lambda: "")
    with pytest.raises(requests.HTTPError):
        dl._get_with_retries("https://fs2.comicfiles.ru/a.cbr", "Download")


# --- folders, MediaFire ---------------------------------------------------------

def test_pixeldrain_folder_comes_down_as_one_zip(monkeypatch):
    monkeypatch.setattr(dl, "_final_hop", lambda u: "https://pixeldrain.com/l/Fold3r")
    assert dl._unwrap_hosted("https://getcomics.org/dls/x") == "https://pixeldrain.com/api/list/Fold3r/zip"


def test_mediafire_scrambled_link_is_decoded(monkeypatch):
    direct = "https://download2267.mediafire.com/abc/yule.cbr"
    page = f'<a id="downloadButton" href="javascript:void(0)" data-scrambled-url="{base64.b64encode(direct.encode()).decode()}">Download</a>'
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: Resp(200, body=page.encode()))
    assert gc.mediafire_direct("https://www.mediafire.com/file/xyz/yule.cbr/file") == direct


# --- every mirror in the group ----------------------------------------------------

def test_group_mirrors_rank_every_host():
    from bs4 import BeautifulSoup
    html = """<div><a href="https://getcomics.org/dls/main">Download Now</a>
      <a href="https://getcomics.org/dls/wt">WETRANSFER</a><a href="https://getcomics.org/dls/mg">MEGA</a>
      <a href="https://getcomics.org/dls/tb">TERABOX</a><a href="https://getcomics.org/dls/mf">MEDIAFIRE</a>
      <a href="https://getcomics.org/dls/pd">PIXELDRAIN</a></div>"""
    c = gc.GetComicsClient.__new__(gc.GetComicsClient)
    c._mirrors = {}
    assert c._remember_mirrors("main", BeautifulSoup(html, "lxml")) == 1
    assert c._mirrors["main"] == ["https://getcomics.org/dls/pd", "https://getcomics.org/dls/mf",
                                  "https://getcomics.org/dls/wt"]


# --- search hygiene ---------------------------------------------------------------

def _page(posts, more):
    arts = "".join(f'<article class="post"><a class="post-category">{cat}</a><h1 class="post-title"><a href="{h}">{t}</a></h1></article>'
                   for t, h, cat in posts)
    nav = '<a class="page-numbers" href="/page/2/">2</a>' if more else '<span class="page-numbers current">1</span>'
    return f"<html>{arts}{nav}</html>"


def test_news_posts_are_skipped_and_page_two_is_read(monkeypatch):
    c = gc.GetComicsClient.__new__(gc.GetComicsClient)
    c._mirrors = {}
    pages = {
        "https://getcomics.org": _page([("Deadpool #8 (2023) news roundup", "https://getcomics.org/news/x", "News"),
                                        ("Deadpool #40 (2023)", "https://getcomics.org/p/40", "Marvel")], True),
        "https://getcomics.org/page/2/": _page([("Deadpool #8 (2023)", "https://getcomics.org/p/8", "Marvel")], False),
    }
    asked = []
    def get(url, params=None, **k):
        asked.append(url)
        return Resp(200, body=pages[url].encode())
    monkeypatch.setattr(c, "_get", get)
    assert c._search_page("Deadpool #8 (2023)", "Deadpool", 8.0, 2023) == "https://getcomics.org/p/8"
    assert asked == ["https://getcomics.org", "https://getcomics.org/page/2/"]


def test_page_two_is_not_asked_when_page_one_has_it(monkeypatch):
    c = gc.GetComicsClient.__new__(gc.GetComicsClient)
    c._mirrors = {}
    asked = []
    def get(url, params=None, **k):
        asked.append(url)
        return Resp(200, body=_page([("Deadpool #8 (2023)", "https://getcomics.org/p/8", "Marvel")], True).encode())
    monkeypatch.setattr(c, "_get", get)
    assert c._search_page("Deadpool #8 (2023)", "Deadpool", 8.0, 2023) == "https://getcomics.org/p/8"
    assert asked == ["https://getcomics.org"]


# --- HD first -------------------------------------------------------------------

def _two_group_post(first, second):
    return f"""<section class="post-contents">
      <p>Deadpool #8 ({first}) Language : English</p>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/{first}">DOWNLOAD NOW</a></div>
      <hr/>
      <p>Deadpool #8 ({second}) Language : English</p>
      <p></p><div class="aio-button-center"><a href="https://getcomics.org/dls/{second}">Download Now</a></div>
      <hr/></section>"""


@pytest.mark.parametrize("first,second,want", [
    ("SD-Digital", "HD-Digital", "HD-Digital"),
    ("HD-Digital", "SD-Digital", "HD-Digital"),
    ("HD-Upscaled", "SD-Digital", "SD-Digital"),
    ("Digital", "Digital-Empire", "Digital"),
])
def test_hd_group_wins(monkeypatch, first, second, want):
    c = gc.GetComicsClient()
    monkeypatch.setattr(c, "_get", lambda url, **k: Resp(200, body=_two_group_post(first, second).encode()))
    monkeypatch.setattr(c, "_prefer_pixeldrain", lambda href: href)
    href, _ = c._extract_download("https://getcomics.org/marvel/deadpool-8-2023/")
    assert href == f"https://getcomics.org/dls/{want}"
