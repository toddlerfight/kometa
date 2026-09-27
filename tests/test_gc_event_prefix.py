"""Event-branded titles — the Batman of Two Worlds case.

LOCG files the one-shot as 'Batman Day 2026: Batman of Two Worlds'. GetComics
posts it as 'Batman of Two Worlds #1 (2026)'. Every query carried the 'Batman Day
2026:' prefix, GetComics' search found nothing for words the post doesn't have,
and the strict matcher would have rejected the post anyway. A year-stamped prefix
is event branding, not part of the book's name.
"""
from kometa import getcomics_client as gc

POST = ('<section class="post-contents"><p>Language : English</p>'
        '<div class="aio-button-center"><a href="https://getcomics.org/dls/two-worlds-1">'
        'Download Now</a></div></section>')


def _search_html(*titles):
    arts = "".join(
        f'<article class="post"><h1 class="post-title"><a href="https://getcomics.org/p/{i}">'
        f'{t}</a></h1></article>' for i, t in enumerate(titles))
    return f"<html><body>{arts}</body></html>"


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


def _fake_site(monkeypatch, c, posts, queries):
    """GetComics-ish search: a post shows up only when every query word is in it."""
    def fake_get(url, **kw):
        if "params" not in kw:
            return _Resp(POST)
        q = kw["params"]["s"]
        queries.append(q)
        words = set(gc._normalize(q).split())
        hits = [p for p in posts if words <= set(gc._normalize(p).split())]
        return _Resp(_search_html(*hits))
    monkeypatch.setattr(c, "_get", fake_get)


def test_event_prefix_falls_back_to_the_subtitle(monkeypatch):
    c = gc.GetComicsClient()
    queries = []
    _fake_site(monkeypatch, c, ["Batman of Two Worlds #1 (2026)"], queries)
    url, _ = c.search("Batman Day 2026: Batman of Two Worlds", 1.0,
                      store_date="2026-09-19", series_year=2026)
    assert url == "https://getcomics.org/dls/two-worlds-1"
    # the full title still goes first — the subtitle is a fallback, not a rewrite
    assert queries[0].startswith("Batman Day 2026: Batman of Two Worlds")


def test_plain_colon_title_is_never_cut_down(monkeypatch):
    # 'Star Wars:' is part of the name. Searching bare 'Legacy Of Vader' would
    # widen the net onto books that aren't this one.
    c = gc.GetComicsClient()
    queries = []
    _fake_site(monkeypatch, c, [], queries)
    c.search("Star Wars: Legacy Of Vader", 1.0, store_date="2025-02-05", series_year=2025)
    assert queries and all(q.startswith("Star Wars: Legacy Of Vader") for q in queries)


def test_subtitle_match_still_rejects_a_spinoff(monkeypatch):
    c = gc.GetComicsClient()
    queries = []
    _fake_site(monkeypatch, c, ["Batman of Two Worlds Sketchbook Edition #1 (2026)"], queries)
    url, _ = c.search("Batman Day 2026: Batman of Two Worlds", 1.0,
                      store_date="2026-09-19", series_year=2026)
    assert url is None
