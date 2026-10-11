"""GetComics tags a single issue with its volume when a name has run more than
once: 'Where Monsters Dwell Vol 2 #2 (2015)'. 'vol' reads as a collected edition
to the single-issue matcher, so #2–4 sat on GetComics 'not found' (2026-10-11).
No network: the search page is fake HTML."""
from kometa import getcomics_client as gc

RESULTS = [
    ("Uncanny X-Men by Gail Simone Vol. 4 – Where Monsters Dwell (TPB) (2026)", "https://g/uxm"),
    ("Where Monsters Dwell Vol. 1 #1 – 38 (1970-1975)", "https://g/v1-pack"),
    ("Where Monsters Dwell – The Phantom Eagle Flies The Savage Skies (TPB) (2015)", "https://g/tpb"),
    ("Where Monsters Dwell Vol. 2 #5 (2015)", "https://g/v2-5"),
    ("Where Monsters Dwell Vol 2 #3 (2015)", "https://g/v2-3"),
    ("Where Monsters Dwell Vol 2 #2 (2015)", "https://g/v2-2"),
]


class _Resp:
    status_code = 200
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


def _client(monkeypatch, results):
    arts = "".join(f'<article class="post"><h1 class="post-title"><a href="{u}">{t}</a></h1></article>'
                   for t, u in results)
    c = gc.GetComicsClient()
    monkeypatch.setattr(c, "_get", lambda url, **kw: _Resp(f"<html><body>{arts}</body></html>"))
    return c


def test_a_volume_tagged_single_is_found(monkeypatch):
    c = _client(monkeypatch, RESULTS)
    assert c._search_page("Where Monsters Dwell #2", "Where Monsters Dwell", 2.0, series_year=2015) == "https://g/v2-2"
    assert c._search_page("Where Monsters Dwell #3", "Where Monsters Dwell", 3.0, series_year=2015) == "https://g/v2-3"


def test_the_other_volume_of_the_name_is_not(monkeypatch):
    c = _client(monkeypatch, [("Where Monsters Dwell Vol 1 #2 (1970)", "https://g/v1-2")])
    assert c._search_page("Where Monsters Dwell #2", "Where Monsters Dwell", 2.0, series_year=2015) is None


def test_trades_and_packs_still_dont_pass_for_a_single(monkeypatch):
    c = _client(monkeypatch, RESULTS[:3])
    assert c._search_page("Where Monsters Dwell #4", "Where Monsters Dwell", 4.0, series_year=2015) is None
