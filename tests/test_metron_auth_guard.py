"""Metron's fail2ban: 3 refused logins in 5 minutes = a 24-hour IP ban (it
happened, 2026-10-10). These pin the two guards so a Settings test, a typo'd
token or a background trickle can never be the third strike. No network:
urlopen is stubbed and counts every request that would have left the house."""
import io
import json
import time
import urllib.error

import pytest

import kometa.db as db
from kometa import main, metron_client as mc


@pytest.fixture
def wired(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: "/comics")
    monkeypatch.setattr(mc, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(mc, "_state", {"last": 0.0, "paused_until": 0.0})
    calls = []

    def urlopen(req, timeout=None):
        tok = req.headers["Authorization"].removeprefix("Bearer ")
        calls.append(tok)
        if tok != "good":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        class R(io.BytesIO):
            headers = {"X-Ratelimit-Sustained-Limit": "5000"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(json.dumps({"results": []}).encode())

    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)
    return dbp, calls


def _token(dbp, tok):
    db.set_config({"metron_token": tok}, dbp)


def test_a_refused_token_is_never_sent_twice(wired):
    dbp, calls = wired
    _token(dbp, "typo-1")
    assert mc.test()[0] is False
    assert mc.test()[0] is False
    assert calls == ["typo-1"]                       # the second test never left the house


def test_a_different_wrong_token_waits_out_the_gap(wired):
    dbp, calls = wired
    _token(dbp, "typo-1")
    mc.test()
    _token(dbp, "typo-2")                            # fixing the typo... with another typo
    ok, detail = mc.test()
    assert ok is False and "try again in" in detail
    assert calls == ["typo-1"]                       # one refusal per window, never three


def test_after_the_gap_a_new_token_is_tried(wired, monkeypatch):
    dbp, calls = wired
    _token(dbp, "typo-1")
    mc.test()
    real = time.time
    monkeypatch.setattr(mc.time, "time", lambda: real() + mc.REFUSAL_GAP_S + 1)
    _token(dbp, "good")
    ok, detail = mc.test()
    assert ok is True and "5000" in detail
    assert calls == ["typo-1", "good"]


def test_settings_endpoint_reports_the_guard(wired):
    dbp, calls = wired
    _token(dbp, "typo-1")
    res = main.test_metron()
    assert res["ok"] is False and res["error"]


def test_disconnect_keeps_the_ban_guard(wired):
    dbp, calls = wired
    _token(dbp, "typo-1")
    mc.test()
    main.disconnect_integration("metron")
    cfg = db.get_config(dbp)
    assert cfg.get("metron_token") == ""
    assert cfg.get(mc.AUTH_REFUSED_KEY)              # the refusal mark survives a disconnect
