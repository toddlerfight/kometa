"""A missing single issue that only lives inside a multi-issue GetComics post.

The Infinity Gauntlet (1991) #1 was missing. GetComics files it as
'Infinity Gauntlet #1 – 6 (1991-1992)' — no leading 'The', an en-dash range, a
year range. Every matcher did a substring test for 'the infinity gauntlet' in the
normalised post title, which has no 'the' in it, so the post was never matched
(not by the singles tiers, not by the pack tier) and the row read 'No result'.
No network: search results and post pages are fake HTML.
"""
import pytest

from kometa import getcomics_client as gc
from kometa.getcomics_client import (_issue_pack_post_covers, _normalize, _pack_post_matches,
                                     _post_covers_issue, _series_matches, _title_leads)

IG = "The Infinity Gauntlet"
IG_NORM = _normalize(IG)
POST = "Infinity Gauntlet #1 – 6 (1991-1992)"
POST_URL = "https://getcomics.org/marvel/infinity-gauntlet-1-6-1991-1992/"
DL = "https://getcomics.org/dls/infinity-gauntlet-1-6"

POST_HTML = f"""
<html><body><section class="post-contents">
<p>Infinity Gauntlet #1 – 6 (1991-1992) : Thanos has the gauntlet...</p>
<p><strong>Infinity Gauntlet #1 – 6</strong> Language : English | Year : 1991-1992 | Size : 400 MB</p>
<div class="aio-button-center"><a href="{DL}">Download Now</a></div>
<hr/>
</section></body></html>
"""


def _search_html(titles_urls):
    arts = "".join(
        f'<article class="post"><h1 class="post-title"><a href="{u}">{t}</a></h1></article>'
        for t, u in titles_urls)
    return f"<html><body>{arts}</body></html>"


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


def _client(monkeypatch, results, post_html=POST_HTML):
    c = gc.GetComicsClient()
    page = _search_html(results)

    def fake_get(url, **kw):
        if "params" in kw:
            return _Resp(page)
        return _Resp(post_html)

    monkeypatch.setattr(c, "_get", fake_get)
    return c


DECOYS = [
    ("Infinity Gauntlet Aftermath #1 (2026)", "https://getcomics.org/p/aftermath"),
    ("Infinity War #1 – 6 (1992)", "https://getcomics.org/p/war"),
]


# ------------------------------------------------------------- the regression


def test_issue_1_is_found_via_the_range_post(monkeypatch):
    c = _client(monkeypatch, DECOYS + [(POST, POST_URL)])
    url, _ = c.search("The Infinity Gauntlet (1991)", 1.0, store_date="1991-07-01", series_year=1991)
    assert url == DL


def test_range_post_reads_as_covering_issue_1():
    assert _series_matches(IG_NORM, _normalize(POST))
    assert _post_covers_issue(POST, 1.0) is True
    assert _issue_pack_post_covers(IG_NORM, POST, 1.0, series_year=1991)


@pytest.mark.parametrize("post", [
    "Infinity Gauntlet #1 – 6 (1991-1992)",
    "Infinity Gauntlet #1-6 (1991-1992)",
    "Infinity Gauntlet #1 — 6 (1991)",
    "Infinity Gauntlet 1 - 6 (1991-1992)",
    "The Infinity Gauntlet #1 – 6 (1991-1992)",
])
def test_range_spellings(post):
    assert _series_matches(IG_NORM, _normalize(post))
    assert _post_covers_issue(post, 1.0) is True
    assert _post_covers_issue(post, 7.0) is False


def test_leading_the_optional_on_either_side():
    assert _series_matches(_normalize("Infinity Gauntlet"), _normalize("The Infinity Gauntlet #1 (1991)"))
    assert _series_matches(IG_NORM, _normalize("Infinity Gauntlet #1 (1991)"))
    assert _pack_post_matches(IG_NORM, POST, 6, series_year=1991)
    assert _title_leads(IG_NORM, _normalize("Infinity Gauntlet (TPB) (2011)"))


def test_year_range_containing_the_series_year_is_accepted():
    assert _issue_pack_post_covers(IG_NORM, POST, 3.0, series_year=1991)


def test_range_post_with_chunks_gives_the_covering_chunk(monkeypatch):
    chunked = """<html><body><section class="post-contents">
    <ul>
    <li>Infinity Gauntlet #1 – 3 (1991) : <a href="https://getcomics.org/dls/ig-1-3">Main Server</a></li>
    <li>Infinity Gauntlet #4 – 6 (1991) : <a href="https://getcomics.org/dls/ig-4-6">Main Server</a></li>
    </ul></section></body></html>"""
    c = _client(monkeypatch, [(POST, POST_URL)], post_html=chunked)
    url, _ = c.search(IG, 5.0, store_date="1991-11-01", series_year=1991)
    assert url == "https://getcomics.org/dls/ig-4-6"


# ------------------------------------------------------------- negatives


def test_range_that_does_not_contain_the_wanted_issue_is_not_taken(monkeypatch):
    c = _client(monkeypatch, [(POST, POST_URL)])
    assert c.search(IG, 7.0, store_date="1992-01-01", series_year=1991) == (None, None)
    assert not _issue_pack_post_covers(IG_NORM, POST, 7.0, series_year=1991)


def test_other_series_sharing_words_are_not_matched(monkeypatch):
    c = _client(monkeypatch, DECOYS)
    assert c.search(IG, 1.0, store_date="1991-07-01", series_year=1991) == (None, None)
    for title, _ in DECOYS:
        assert not _series_matches(IG_NORM, _normalize(title))
        assert not _issue_pack_post_covers(IG_NORM, title, 1.0, series_year=1991)


def test_range_post_from_a_different_run_is_not_taken():
    assert not _issue_pack_post_covers(IG_NORM, "Infinity Gauntlet #1 – 5 (2015-2016)", 1.0,
                                       series_year=1991)
