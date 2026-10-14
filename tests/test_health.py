"""Source health (kometa/health.py): set up fine, failing anyway → a red dot. No network."""
import pytest

import kometa.db as db
from kometa import health, main, metron_client, polite


@pytest.fixture
def h(db_path, monkeypatch):
    monkeypatch.setattr(health, "PERSIST", True)
    health.reset()
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(health, "_now", lambda: t["now"])
    yield t
    health.reset()


def test_one_bad_minute_is_not_news(h, db_path):
    for _ in range(10):
        health.record("Metron", "error", "503", db_path)
    h["now"] += 60
    assert health.unwell(db_path, h["now"]) == []


def test_an_evening_of_refusals_lights_the_card(h, db_path):
    health.record("Images static.wikia.nocookie.net", "ok", path=db_path)
    for _ in range(6):
        h["now"] += 600
        health.record("Images static.wikia.nocookie.net", "refused", "403", db_path)
    [u] = health.unwell(db_path, h["now"])
    assert u["card"] == "fandom" and "refusing us" in u["reason"] and "6 in a row" in u["reason"]


def test_a_success_clears_it(h, db_path):
    for _ in range(6):
        h["now"] += 600
        health.record("GCD", "error", "timeout", db_path)
    health.record("GCD", "ok", path=db_path)
    assert health.unwell(db_path, h["now"]) == []


def test_resting_over_a_day_lights_it_even_with_few_attempts(h, db_path):
    health.record("LOCG", "refused", "403", db_path)
    h["now"] += 25 * 3600
    assert [u["card"] for u in health.unwell(db_path, h["now"])] == ["locg"]


def test_state_survives_a_restart(h, db_path):
    health.record("PRH", "refused", "429", db_path)        # a change of state: written
    health.reset()                                         # a new process
    h["now"] += 25 * 3600
    assert [u["card"] for u in health.unwell(db_path, h["now"])] == ["prh"]


def test_the_limiter_reports_every_answer(h, db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    lim = polite.Limiter("Open Library", min_interval_s=0, daily_cap=10)
    lim.reset()

    class R:
        def __init__(self, code): self.status_code, self.headers = code, {}
    codes = iter([200, 404, 429])
    monkeypatch.setattr(polite.requests, "get", lambda *a, **k: R(next(codes)))
    lim.get("https://x/1"); lim.get("https://x/2")
    assert health.snapshot(db_path)["Open Library"]["streak"] == 0      # a 404 is an answer
    with pytest.raises(polite.SourceUnavailable):
        lim.get("https://x/3")
    assert health.snapshot(db_path)["Open Library"]["kind"] == "refused"


def test_settings_badge_shows_an_unwell_source_only_while_its_switched_on(h, db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    root = tmp_path / "comics"; root.mkdir()
    monkeypatch.setattr(main, "_comics_root", lambda: str(root))
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": True, "locg_topup_waiting": 0})
    db.set_config({"metron_token": "t", "cv_api_key": "k", "gcd_user": "u", "gcd_pass": "p", "prowlarr_enabled": "0",
                   "hardcover_token": "h"}, db_path)
    monkeypatch.setattr(metron_client, "_refused", lambda c: False)
    for _ in range(6):
        h["now"] += 600
        health.record("ComicBookRoundup", "error", "timeout", db_path)
    [cbr] = [i for i in main.settings_attention() if i["card"] == "cbr"]
    assert cbr["title"] == "ComicBookRoundup" and "retrying" in cbr["fix"]
    for _ in range(6):
        h["now"] += 600
        health.record("Images files1.comics.org", "refused", "403", db_path)
    [gcd] = [i for i in main.settings_attention() if i["card"] == "gcd"]
    assert gcd["reason"].startswith("Cover images from files1.comics.org: refusing us") and "backs off" in gcd["fix"]
    db.set_config({"cbr_enabled": "0"}, db_path)
    assert not any(i["card"] == "cbr" for i in main.settings_attention())
