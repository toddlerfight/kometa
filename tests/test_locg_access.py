"""Your own browser's pass for LOCG (kometa/locg_client.py, 2026-10-08): a
an access token from Settings rides the trickle; a refusal drops it."""
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
    assert r["ok"] and r["with_pass"] and "pause lifted" in r["detail"]
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
    assert not r["ok"] and "refused" in r["detail"]
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


def test_a_knock_while_paused_does_not_extend_the_pause(wired, monkeypatch):
    import time
    until = time.time() + 1800                                # paused for another 30 min
    lc._pause["until"] = until
    orig_init = FakeSession.__init__
    def refusing(self, **k):
        orig_init(self, **k)
        self.status, self.resp_headers = 403, {"cf-mitigated": "challenge"}
    monkeypatch.setattr(FakeSession, "__init__", refusing)
    lc.probe()
    assert lc._pause["until"] == until                        # same clock, not +3h


def test_probe_matrix_tries_handshakes_until_one_passes_and_forgets_nothing(wired, monkeypatch):
    db.set_config({"locg_cf_clearance": "abc", "locg_user_agent": "Chrome/154"}, wired)
    seen = []
    orig_init = FakeSession.__init__
    def init(self, impersonate="chrome", **k):
        orig_init(self, **k)
        seen.append(impersonate)
        self.status, self.resp_headers = (200, {}) if impersonate == "chrome145" else (403, {"cf-mitigated": "challenge"})
    monkeypatch.setattr(FakeSession, "__init__", init)
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    r = lc.probe_matrix()
    assert [x["status"] for x in r] == [403, 403, 200] and seen[-1] == "chrome145"
    assert db.get_config(wired).get("locg_cf_clearance") == "abc"          # a refusal in the matrix forgets nothing
    db.set_config({"locg_impersonate": "chrome145"}, wired)
    lc._access_cache["ts"] = 0.0
    assert lc._impersonation() == "chrome145"


def test_flaresolverr_clears_locg_without_manual_token(wired, monkeypatch):
    import kometa.flaresolverr as fs
    monkeypatch.setattr(fs, "_base", lambda: "http://nas:8191")
    monkeypatch.setattr(fs, "clearance", lambda url: ("UA-Flare", {"cf_clearance": "auto_clearance_123"}))

    calls = []
    def fake_fs_get(url, **kw):
        calls.append(url)
        return lc.FlareSolverrResponse(200, "<ul><li class='media'><a href='/comics/series/1/'>Batman</a><span class='title'>Batman</span></li></ul>")

    monkeypatch.setattr(lc, "_flaresolverr_get", fake_fs_get)

    # 1. _access() automatically gets clearance from FlareSolverr without manual token
    token, ua = lc._access()
    assert token == "auto_clearance_123" and ua == "UA-Flare"

    # 2. probe() routes through FlareSolverr
    r = lc.probe()
    assert r["ok"] and r["via_flaresolverr"] and "FlareSolverr" in r["detail"]

    # 3. _anon_get_fn() routes requests through FlareSolverr
    fn = lc._anon_get_fn()
    res = fn(f"{lc.BASE}/search/ajax_issues", params={"query": "Batman"})
    assert res.status_code == 200
    assert any("search/ajax_issues" in c for c in calls)


def test_flaresolverr_json_cleaning():
    resp_raw = lc.FlareSolverrResponse(200, '{"list": "ok"}')
    assert resp_raw.json() == {"list": "ok"}

    resp_html = lc.FlareSolverrResponse(200, '<html><body><pre>{"list": "wrapped"}</pre></body></html>')
    assert resp_html.json() == {"list": "wrapped"}

