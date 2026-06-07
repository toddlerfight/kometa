"""Taste profile — aggregate cached creators across the library into top creators.
Seeded cache, no network."""
import kometa.db as db
import kometa.recommend as rec


def _seed(path, title, locg_issue_id, credits):
    sid = db.add_series(komga_series_id=None, metron_series_id=None, title=title,
                        publisher="X", locg_series_id=1, path=path)
    db.upsert_issue_status(sid, 1.0, None, owned=False, locg_issue_id=locg_issue_id, path=path)
    db.set_issue_details_cache(locg_issue_id, {"desc": "d", "credits": credits}, path)
    return sid


def _w(name, pid):
    return {"role": "Writer", "name": name, "people_id": pid, "people_slug": name.lower()}


class TestTasteProfile:
    def test_ranks_by_series_count_and_excludes_editorial(self, tmp_path):
        p = str(tmp_path / "k.db")
        db.init_db(p)
        editor = {"role": "Editor", "name": "An Editor", "people_id": "999", "people_slug": "ed"}
        _seed(p, "Saga", "c1", [_w("BKV", "1"), editor])
        _seed(p, "Paper Girls", "c2", [_w("BKV", "1")])
        _seed(p, "Batman", "c3", [_w("Snyder", "2")])

        prof = rec.taste_profile(p, min_series=1)
        top = prof[0]
        assert top["name"] == "BKV" and top["series_count"] == 2
        assert top["series"] == ["Paper Girls", "Saga"]
        assert all(c["name"] != "An Editor" for c in prof)   # editorial filtered out

    def test_min_series_filters_one_offs(self, tmp_path):
        p = str(tmp_path / "k.db")
        db.init_db(p)
        _seed(p, "Solo", "c1", [_w("One Off", "7")])
        assert rec.taste_profile(p, min_series=2) == []        # only on one series
        assert len(rec.taste_profile(p, min_series=1)) == 1

    def test_creator_counted_once_per_series(self, tmp_path):
        p = str(tmp_path / "k.db")
        db.init_db(p)
        # same creator credited twice on the same issue (Writer + Cover) = one series
        _seed(p, "Wytches", "c1", [
            {"role": "Writer", "name": "Snyder", "people_id": "2", "people_slug": "snyder"},
            {"role": "Cover Artist", "name": "Snyder", "people_id": "2", "people_slug": "snyder"},
        ])
        prof = rec.taste_profile(p, min_series=1)
        assert prof[0]["name"] == "Snyder"
        assert prof[0]["series_count"] == 1
