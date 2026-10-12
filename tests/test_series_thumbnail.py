"""Series card cover fallback — 2026-10-06, 'Batman: Bad Seeds': a LOCG entry
holding only its TPB + HC. No issues meant no art, and the card was a black void."""
import pytest
from fastapi import HTTPException

import kometa.db as db
import kometa.thumbnails as th

TPB = "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-2050784.jpg"


@pytest.fixture
def wired(db_path, series, monkeypatch):
    monkeypatch.setattr(th, "DB_PATH", db_path)
    served = []
    monkeypatch.setattr(th, "_cached_image_response", lambda url, *a, **k: served.append(url) or url)
    return db_path, series, served


def test_collections_only_series_falls_back_to_trade_cover(wired):
    db_path, series, served = wired
    db.set_trades(series, [
        {"format": "TPB", "cover": f"{TPB}?var", "is_variant": True},   # variants never front the card
        {"format": "TPB", "cover": TPB, "is_variant": False},
        {"format": "HC", "cover": "https://x/hc.jpg", "is_variant": False},
    ], db_path)
    assert th.series_thumbnail(series) == TPB


def test_issue_art_still_wins_over_trades(wired):
    db_path, series, served = wired
    db.upsert_issue_status(series, 1.0, "2026-01-01", owned=False,
                           metron_image="https://x/issue1.jpg", path=db_path)
    db.set_trades(series, [{"format": "TPB", "cover": TPB, "is_variant": False}], db_path)
    assert th.series_thumbnail(series) == "https://x/issue1.jpg"


def test_nothing_anywhere_still_404s(wired):
    _, series, _ = wired
    with pytest.raises(HTTPException) as e:
        th.series_thumbnail(series)
    assert e.value.status_code == 404


def test_a_cover_not_ready_answers_at_once_and_keeps_cooking():
    import threading
    import kometa.thumbnails as th
    gate, made = threading.Event(), []
    def slow():
        gate.wait(5)
        made.append(1)
        return "cover"
    assert th._within("t-slow", slow).status_code == 204      # not ready: no waiting on the share
    gate.set()
    th._inflight.get("t-slow") and th._inflight["t-slow"].result(5)
    assert made == [1]                                        # it finished anyway, for next time
    assert th._within("t-fast", lambda: "ready") == "ready"
