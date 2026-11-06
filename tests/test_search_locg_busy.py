"""/api/search/locg tells 'LOCG has nothing' apart from 'LOCG didn't answer'.

Bedford Street, 2026-11-06: the search lost its turn behind the owner's own
earlier keystroke, came back [] like a verdict, and the wizard showed Comic
Vine's near-misses instead of the series LOCG actually had.
"""
import pytest
from fastapi import HTTPException

import kometa.locg_client as locg
import kometa.main as main


def _raise(exc):
    def f(q):
        raise exc
    return f


def test_busy_is_a_503_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(locg, "search_series_strict", _raise(locg.LocgBusy("LOCG is busy with background work")))
    with pytest.raises(HTTPException) as e:
        main.search_locg("bedford street")
    assert e.value.status_code == 503


def test_a_broken_answer_is_a_503(monkeypatch):
    monkeypatch.setattr(locg, "search_series_strict", _raise(TimeoutError("FlareSolverr timed out")))
    with pytest.raises(HTTPException) as e:
        main.search_locg("bedford street")
    assert e.value.status_code == 503


def test_switched_off_or_paused_stays_empty(monkeypatch):
    # knocking again changes nothing — the page shouldn't be told to retry
    monkeypatch.setattr(locg, "search_series_strict", _raise(locg.LocgPaused("LOCG is switched off in Settings")))
    assert main.search_locg("bedford street") == []


def test_an_answer_still_comes_through(monkeypatch):
    monkeypatch.setattr(locg, "search_series_strict", lambda q: [
        {"id": 9106125, "title": "Bedford Street TP", "publisher": "Fantagraphics Books", "year": 2026,
         "comic": True, "slug": "bedford-street-tp"},
        {"id": 217784, "title": "Bedford Street", "publisher": "Fantagraphics Books", "year": 2026},
    ])
    rows = main.search_locg("bedford street")
    assert [r["id"] for r in rows] == [217784, 9106125]      # series before the one-shot
    assert rows[1]["needs_resolve"] is True
