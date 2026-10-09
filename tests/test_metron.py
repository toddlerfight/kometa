"""Metron as the primary source (docs/reader-spec.md, 2026-10-08)."""
import io
import json
import urllib.error

import pytest

import kometa.db as db
import kometa.metron_client as mc
import kometa.shelf_import as si
import kometa.sync as sync


class TestClient:
    def test_title_variants_turn_folder_dashes_into_colons(self):
        assert mc.title_variants("Hellblazer - Bad Blood") == ["Hellblazer - Bad Blood", "Hellblazer: Bad Blood"]
        assert mc.title_variants("Joker- Killer Smile") == ["Joker- Killer Smile", "Joker: Killer Smile"]
        assert mc.title_variants("Batman (2016)") == ["Batman"]
        assert mc.title_variants("Spider-Gwen") == ["Spider-Gwen"]           # inner hyphen untouched

    def test_429_pauses_every_caller_and_is_not_no_results(self, monkeypatch):
        monkeypatch.setattr(mc, "_creds", lambda: ("u", "p"))
        monkeypatch.setattr(mc, "_state", {"last": 0.0, "paused_until": 0.0})
        monkeypatch.setattr(mc, "MIN_INTERVAL_S", 0)
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 429, "slow down", {"Retry-After": "120"}, io.BytesIO(b""))
        monkeypatch.setattr(mc.urllib.request, "urlopen", boom)
        with pytest.raises(mc.MetronUnavailable):
            mc.search_series("Saga")
        with pytest.raises(mc.MetronUnavailable, match="wait"):
            mc.search_series("Saga")                    # paused: doesn't even call out

    def test_daily_floor_pauses_until_reset(self, monkeypatch):
        monkeypatch.setattr(mc, "_creds", lambda: ("u", "p"))
        monkeypatch.setattr(mc, "_state", {"last": 0.0, "paused_until": 0.0})
        monkeypatch.setattr(mc, "MIN_INTERVAL_S", 0)
        class R(io.BytesIO):
            headers = {"X-Ratelimit-Sustained-Remaining": "12", "X-Ratelimit-Sustained-Reset": "4102444800"}
            def __enter__(self): return self
            def __exit__(self, *a): pass
        monkeypatch.setattr(mc.urllib.request, "urlopen",
                            lambda *a, **k: R(json.dumps({"results": [], "next": None}).encode()))
        mc.search_series("Saga")
        assert mc._state["paused_until"] == 4102444800


def _mrow(id_, title, pub, year):
    return {"id": id_, "title": title, "publisher": pub, "year": year}


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    for mod in (si, sync):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(sync, "_komga", lambda: None)
    monkeypatch.setattr(mc, "configured", lambda: True)
    import kometa.locg_client as lc
    monkeypatch.setattr(lc, "_pause", {"until": 0.0})      # LOCG open: _match may fall back to it
    return db_path


class TestMatching:
    def test_metron_first_and_locg_never_asked_when_metron_is_sure(self, lib, monkeypatch):
        monkeypatch.setattr(mc, "search_series", lambda t: [_mrow(7, "Saga", "Image", 2012)])
        monkeypatch.setattr(si, "find_confident_match", lambda s, **k: pytest.fail("asked LOCG"))
        assert si._match({"title": "Saga", "publisher": "Image Comics", "folder_path": "/nope"}) == (7, None)

    def test_falls_back_to_locg_when_metron_unsure(self, lib, monkeypatch):
        monkeypatch.setattr(mc, "search_series", lambda t: [])
        monkeypatch.setattr(si, "find_confident_match", lambda s, **k: 99)
        assert si._match({"title": "Tintin", "publisher": "Casterman", "folder_path": "/nope"}) == (None, 99)

    def test_metron_down_is_try_later(self, lib, monkeypatch):
        def down(t):
            raise mc.MetronUnavailable("503")
        monkeypatch.setattr(mc, "search_series", down)
        with pytest.raises(mc.MetronUnavailable):
            si._match({"title": "Saga", "publisher": "Image", "folder_path": "/nope"})

    def test_existing_series_gain_a_metron_link_pull_list_first(self, lib, monkeypatch):
        a = db.add_series(title="Saga", publisher="Image", on_pull_list=False, path=lib)
        b = db.add_series(title="Absolute Batman", publisher="DC Comics", on_pull_list=True, path=lib)
        asked = []
        def search(t):
            asked.append(t)
            return [_mrow(1, "Absolute Batman", "DC Comics", 2024)] if "Batman" in t else []
        monkeypatch.setattr(mc, "search_series", search)
        assert si.link_metron_existing(limit=5) == 1
        assert asked[0] == "Absolute Batman"                                  # pull list first
        assert db.get_series_by_id(b, lib)["metron_series_id"] == 1
        assert db.get_series_by_id(a, lib)["metron_link"] == "none"
        assert si.link_metron_existing(limit=5) == 0 and len(asked) == 2      # 'none' isn't re-asked


class TestSync:
    def _series(self, lib, pull):
        sid = db.add_series(title="Absolute Batman", publisher="DC", on_pull_list=pull, path=lib)
        db.set_metron_series_id(sid, 1, lib)
        db.set_locg_series_id(sid, 2, lib)
        return sid

    def _wire(self, monkeypatch):
        calls = {"metron": 0, "locg": 0}
        def metron_issues(mid):
            calls["metron"] += 1
            return [{"number": 24.0, "store_date": "2026-09-23", "image": "https://m/24.jpg", "metron_issue_id": 5}]
        def locg_issues(lid):
            calls["locg"] += 1
            return [{"number": 26.0, "store_date": "2026-11-25", "cover": None, "locg_issue_id": "8"}]
        monkeypatch.setattr(mc, "series_issues", metron_issues)
        monkeypatch.setattr(sync, "get_issues_anon", locg_issues)
        monkeypatch.setattr(sync, "get_trades_anon", lambda lid: [])
        return calls

    def test_metron_list_plus_locg_far_solicits_weekly_for_pulled(self, lib, monkeypatch):
        calls = self._wire(monkeypatch)
        sid = self._series(lib, pull=True)
        for _ in range(3):
            sync.sync_one(db.get_series_by_id(sid, lib))
        assert calls["metron"] == 3 and calls["locg"] == 1           # ongoing: Metron each time, LOCG weekly
        assert {i["number"] for i in db.get_issues_for_series(sid, lib)} == {24.0, 26.0}

    def test_not_pulled_metron_series_never_asks_locg_for_issues(self, lib, monkeypatch):
        calls = self._wire(monkeypatch)
        sid = self._series(lib, pull=False)
        db.set_locg_fetched(sid, "2026-01-01 00:00:00", lib)        # pretend it had a LOCG fetch once
        sync.sync_one(db.get_series_by_id(sid, lib))
        assert calls["locg"] == 0


def test_burst_exhausted_pauses_until_its_reset_without_a_429(monkeypatch):
    """Metron's own guidance: when Burst-Remaining reads 0, stop until Burst-Reset —
    don't fire the next request and get the 429 (and the ban that follows abuse)."""
    import io, json, time, urllib.request
    import kometa.metron_client as m
    monkeypatch.setattr(m, "_creds", lambda: ("token", "t"))
    monkeypatch.setattr(m, "MIN_INTERVAL_S", 0)
    m._state["paused_until"] = 0.0
    class R(io.BytesIO):
        status = 200
        headers = {"X-Ratelimit-Burst-Limit": "20", "X-Ratelimit-Burst-Remaining": "0",
                   "X-Ratelimit-Burst-Reset": str(int(time.time()) + 30),
                   "X-Ratelimit-Sustained-Remaining": "4000", "X-Ratelimit-Sustained-Reset": str(int(time.time()) + 3600)}
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: R(json.dumps({"results": []}).encode()))
    assert m._get("publisher/", name="Image") == {"results": []}
    assert m._state["paused_until"] > time.time() + 20
    import pytest
    with pytest.raises(m.MetronUnavailable):
        m._get("publisher/", name="Image")                      # the next call waits, it doesn't knock
    m._state["paused_until"] = 0.0


def test_unreachable_trips_a_breaker_so_callers_fail_fast(monkeypatch):
    import urllib.request, urllib.error, time
    import kometa.metron_client as m
    monkeypatch.setattr(m, "_creds", lambda: ("token", "t"))
    monkeypatch.setattr(m, "MIN_INTERVAL_S", 0)
    m._state["paused_until"] = 0.0
    calls = []
    def dead(req, timeout=30):
        calls.append(1); raise urllib.error.URLError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", dead)
    import pytest
    with pytest.raises(m.MetronUnavailable):
        m._get("publisher/", name="Image")
    with pytest.raises(m.MetronUnavailable):
        m._get("publisher/", name="Image")                      # no second network attempt
    assert len(calls) == 1 and m._state["paused_until"] > time.time() + 200
    m._state["paused_until"] = 0.0
