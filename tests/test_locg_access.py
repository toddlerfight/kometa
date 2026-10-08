"""Your own browser's pass for LOCG (kometa/locg_client.py, 2026-10-08): a
cf_clearance cookie pasted from Settings rides the trickle; a refusal forgets it."""
import sys
import types

import pytest

import kometa.db as db
import kometa.locg_client as lc


class FakeSession:
    made = []

    def __init__(self, **k):
        self.headers = {}
        self.cookies = types.SimpleNamespace(set=lambda name, value, domain=None: self.headers.__setitem__(f"cookie:{name}", value))
        self.status = 200
        self.resp_headers = {}
        FakeSession.made.append(self)

    def get(self, *a, **k):
        s = self
        class R:
            status_code = s.status
            headers = s.resp_headers
            text = ""
        return R()

    def close(self):
        pass


@pytest.fixture
def wired(db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(lc, "_pause", {"until": 0.0})
    monkeypatch.setattr(lc, "_access_cache", {"ts": 0.0, "val": ("", "")})
    monkeypatch.setattr(lc, "_anon_session", {"session": None, "get": None, "ts": 0.0})
    FakeSession.made = []
    monkeypatch.setitem(sys.modules, "curl_cffi", types.SimpleNamespace(requests=types.SimpleNamespace(Session=FakeSession)))
    return db_path


def test_probe_carries_the_pasted_pass_and_its_user_agent(wired):
    db.set_config({"locg_cf_clearance": "abc123", "locg_user_agent": "Mozilla/5.0 Chrome/154"}, wired)
    r = lc.probe()
    s = FakeSession.made[-1]
    assert r["ok"] and r["with_pass"] and "your browser" in r["detail"]
    assert s.headers["cookie:cf_clearance"] == "abc123" and s.headers["User-Agent"] == "Mozilla/5.0 Chrome/154"


def test_no_pass_means_plain_request(wired):
    r = lc.probe()
    assert r["ok"] and not r["with_pass"]
    assert "cookie:cf_clearance" not in FakeSession.made[-1].headers


def test_refusal_with_a_pass_forgets_it_and_pauses(wired, monkeypatch):
    db.set_config({"locg_cf_clearance": "dead", "locg_cf_set_at": "2026-10-08 05:00:00"}, wired)
    orig_init = FakeSession.__init__
    def refusing(self, **k):
        orig_init(self, **k)
        self.status, self.resp_headers = 403, {"cf-mitigated": "challenge"}
    monkeypatch.setattr(FakeSession, "__init__", refusing)
    r = lc.probe()
    assert not r["ok"] and "expired" in r["detail"]
    cfg = db.get_config(wired)
    assert cfg.get("locg_cf_clearance") == "" and cfg.get("locg_cf_set_at") == ""
    assert lc.locg_paused()                                   # back to waiting, as before


def test_anon_session_rebuilds_when_the_pass_changes(wired):
    g1 = lc._anon_get_fn()
    assert len(FakeSession.made) == 1 and "cookie:cf_clearance" not in FakeSession.made[0].headers
    db.set_config({"locg_cf_clearance": "fresh"}, wired)
    lc._access_cache["ts"] = 0.0
    g2 = lc._anon_get_fn()
    assert len(FakeSession.made) == 2 and FakeSession.made[1].headers["cookie:cf_clearance"] == "fresh"
    assert lc._anon_get_fn() is g2 and len(FakeSession.made) == 2   # same pass → no rebuild
