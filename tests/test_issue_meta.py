"""Issue details + variants from Metron, LOCG as the fallback (kometa/issue_meta.py)."""
import pytest

import kometa.db as db
import kometa.metron_client as mc
import kometa.issue_meta as im
import kometa.downloader as dl


METRON_ISSUE = {
    "id": 182969, "number": "25", "desc": "All the villains.", "store_date": "2026-10-28",
    "foc_date": "2026-09-28",
    "image": "https://static.metron.cloud/media/issue/2026/09/07/ae36.jpg",
    "credits": [
        {"id": 172, "creator": "Jim Lee", "role": [{"id": 19, "name": "President"}, {"id": 31, "name": "Publisher"}]},
        {"id": 7, "creator": "Scott Snyder", "role": [{"id": 1, "name": "Writer"}]},
        {"id": 8, "creator": "Nick Dragotta", "role": [{"id": 2, "name": "Artist"}, {"id": 3, "name": "Cover"}]},
    ],
    "arcs": [{"id": 1, "name": "Zur-En-Arrh"}],
    "variants": [
        {"name": "Cover B J. Scott Campbell Variant", "image": "https://static.metron.cloud/media/variants/e486.jpg"},
        {"name": "Cover C Capullo", "image": "https://static.metron.cloud/media/variants/9f1a.jpg"},
        {"name": "no image yet", "image": None},
    ],
}


class TestMetronIssueDetail:
    def test_maps_to_the_shape_the_modal_already_eats(self, monkeypatch):
        monkeypatch.setattr(mc, "_get", lambda path, **k: METRON_ISSUE)
        d = mc.issue_detail(182969)
        assert d["desc"] == "All the villains."
        roles = {(c["role"], c["name"]) for c in d["credits"]}
        assert ("Writer", "Scott Snyder") in roles and ("Cover", "Nick Dragotta") in roles
        assert not any(c["name"] == "Jim Lee" for c in d["credits"])          # masthead dropped
        assert [c["name"] for c in d["covers"]] == ["Cover A (Main)", "Cover B J. Scott Campbell Variant", "Cover C Capullo"]
        assert d["covers"][0]["id"] == "m182969" and d["covers"][1]["id"] == "e486"
        assert d["covers"][1]["large"] == "https://static.metron.cloud/media/variants/e486.jpg"
        assert d["arcs"] == ["Zur-En-Arrh"] and d["foc_date"] == "2026-09-28"


@pytest.fixture
def lib(db_path, monkeypatch):
    monkeypatch.setattr(im, "DB_PATH", db_path)
    monkeypatch.setattr(mc, "configured", lambda: True)
    return db_path


class TestResolver:
    def test_metron_answers_and_is_cached(self, lib, monkeypatch):
        calls = []
        monkeypatch.setattr(mc, "issue_detail", lambda mid: (calls.append(mid), {"desc": "d", "credits": [], "covers": [{"id": "x"}]})[1])
        issue = {"metron_issue_id": 5, "locg_issue_id": "9"}
        assert im.details_for(issue, lib, want_covers=True)["source"] == "metron"
        assert im.details_for(issue, lib)["desc"] == "d"
        assert calls == [5]                                            # second call hit the cache

    def test_locg_only_when_metron_has_nothing(self, lib, monkeypatch):
        import kometa.locg_client as lc
        monkeypatch.setattr(lc, "get_issue_details_anon", lambda lid: {"desc": "from locg", "credits": []})
        monkeypatch.setattr(lc, "fetch_variants", lambda lid: {"covers": [{"id": "77"}]})
        d = im.details_for({"metron_issue_id": None, "locg_issue_id": "77"}, lib, want_covers=True)
        assert d["source"] == "locg" and d["covers"] == [{"id": "77"}]

    def test_metron_down_falls_back_to_stale_cache_then_locg(self, lib, monkeypatch):
        import kometa.locg_client as lc
        db.set_issue_details_cache("metron:5", {"desc": "old", "credits": [], "covers": []}, lib)
        def down(mid):
            raise mc.MetronUnavailable("paused")
        monkeypatch.setattr(mc, "issue_detail", down)
        assert im.details_for({"metron_issue_id": 5}, lib)["desc"] == "old"
        # LOCG shut too → the stale Metron answer still beats a 502
        def shut(lid):
            raise RuntimeError("403")
        monkeypatch.setattr(lc, "get_issue_details_anon", shut)
        assert im.details_for({"metron_issue_id": 5, "locg_issue_id": "1"}, lib)["desc"] == "old"

    def test_no_source_can_answer_raises(self, lib, monkeypatch):
        import kometa.locg_client as lc
        def shut(lid):
            raise RuntimeError("403")
        monkeypatch.setattr(lc, "get_issue_details_anon", shut)
        with pytest.raises(RuntimeError):
            im.details_for({"metron_issue_id": None, "locg_issue_id": "1"}, lib)

    def test_cache_age_window(self, lib):
        db.set_issue_details_cache("metron:1", {"desc": "x"}, lib)
        assert db.get_issue_details_cache("metron:1", lib, max_age_days=7) == {"desc": "x"}
        assert db.get_issue_details_cache("metron:1", lib, max_age_days=-1) is None


class TestPlumbing:
    def test_upsert_keeps_metron_issue_id(self, db_path):
        sid = db.add_series(title="Absolute Batman", publisher="DC", path=db_path)
        db.upsert_issue_status_many([(sid, 25.0, "2026-10-28", False, None, "img", None, 182969)], path=db_path)
        db.upsert_issue_status_many([(sid, 25.0, "2026-10-28", False, None, "img", "locg9")], path=db_path)  # 7-tuple
        row = db.get_issues_for_series(sid, db_path)[0]
        assert row["metron_issue_id"] == 182969 and row["locg_issue_id"] == "locg9"

    def test_cover_download_uses_the_covers_own_url(self, monkeypatch):
        seen = []
        class R:
            status_code, content = 200, b"jpg"
        monkeypatch.setattr(dl.requests, "get", lambda url, timeout: (seen.append(url), R())[1])
        dl._download_cover({"id": "e486", "large": "https://static.metron.cloud/v/e486.jpg"})
        dl._download_cover({"id": "123"})
        dl._download_cover("456")
        assert seen == ["https://static.metron.cloud/v/e486.jpg",
                        dl.S3_LARGE.format("123"), dl.S3_LARGE.format("456")]


class TestLocgTopUp:
    def test_locg_variants_added_on_top_of_metron_when_open(self, lib, monkeypatch):
        import kometa.locg_client as lc
        monkeypatch.setattr(mc, "issue_detail", lambda mid: {"desc": "d", "credits": [],
                            "covers": [{"id": "m1", "name": "Cover A (Main)"}, {"id": "v1", "name": "Cover B"}]})
        monkeypatch.setattr(lc, "locg_paused", lambda: None)
        monkeypatch.setattr(lc, "fetch_variants", lambda lid: {"covers": [
            {"id": "77", "name": "Cover A (Main)"}, {"id": "78", "name": "Cover B"}, {"id": "79", "name": "Retailer Exclusive"}]})
        d = im.details_for({"metron_issue_id": 1, "locg_issue_id": "77"}, lib, want_covers=True)
        assert d["source"] == "metron+locg"
        assert [c["id"] for c in d["covers"]] == ["m1", "v1", "79"]      # LOCG's main + dup name skipped

    def test_locg_skipped_entirely_while_paused(self, lib, monkeypatch):
        import kometa.locg_client as lc
        monkeypatch.setattr(mc, "issue_detail", lambda mid: {"desc": "d", "credits": [], "covers": [{"id": "m1", "name": "Cover A (Main)"}]})
        monkeypatch.setattr(lc, "locg_paused", lambda: 4102444800.0)
        def boom(lid):
            raise AssertionError("LOCG must not be asked while paused")
        monkeypatch.setattr(lc, "fetch_variants", boom)
        d = im.details_for({"metron_issue_id": 2, "locg_issue_id": "77"}, lib, want_covers=True)
        assert d["source"] == "metron" and len(d["covers"]) == 1
