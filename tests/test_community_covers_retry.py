"""Community pick covers that missed get another look (kometa/community_covers.py):
Immortal Hulk was asked under a bogus 1970 and X-Men's run was crowded off Metron's
first page (2026-10-17)."""
import kometa.db as db
from kometa import community_covers as cc


class Metron:
    class MetronUnavailable(Exception):
        pass

    def __init__(self, by_year, plain):
        self.by_year, self.plain, self.calls = by_year, plain, []

    def configured(self):
        return True

    def harvest_share_spent(self):
        return False

    def search_series(self, title, year=None):
        self.calls.append(("search", title, year))
        return self.by_year.get(year, []) if year else self.plain

    def _get(self, path, **params):
        return {"results": [{"number": "1", "image": "https://static.metron.cloud/cover-1.jpg"}]}

    @staticmethod
    def _num(n):
        return float(n)


HULK = "/comic-books/reviews/marvel-comics/immortal-hulk"
XMEN = "/comic-books/reviews/marvel-comics/x-men-(2019)"


def _pick(path, name, year):
    return {"series_path": path, "name": name, "year": year, "publisher": "marvel-comics"}


def test_a_miss_is_asked_again_once_its_year_is_put_right(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    cc.enrich([_pick(HULK, "Immortal Hulk", 1970)], db_path)
    m = Metron({}, [{"id": 7, "title": "Immortal Hulk", "publisher": "Marvel", "year": 2018}])
    cc.tick(path=db_path, client=m)
    [it] = cc.enrich([_pick(HULK, "Immortal Hulk", 1970)], db_path)
    assert "cover" not in it                                              # 1970 found nothing, and isn't re-asked
    assert cc.tick(path=db_path, client=m) == 0
    cc.enrich([_pick(HULK, "Immortal Hulk", 2018)], db_path)               # the year is right now
    assert cc.tick(path=db_path, client=m) == 1
    [it] = cc.enrich([_pick(HULK, "Immortal Hulk", 2018)], db_path)
    assert it["metron_series_id"] == 7 and it["cover"].endswith("cover-1.jpg")


def test_a_crowded_search_looks_again_by_year(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    cc.enrich([_pick(XMEN, "X-Men", 2019)], db_path)
    spinoffs = [{"id": 100 + i, "title": f"X-Men Spin-off {i}", "publisher": "Marvel", "year": 2019} for i in range(100)]
    m = Metron({2019: [{"id": 55, "title": "X-Men", "publisher": "Marvel", "year": 2019}]}, spinoffs)
    assert cc.tick(path=db_path, client=m) == 1
    assert ("search", "X-Men", 2019) in m.calls
    [it] = cc.enrich([_pick(XMEN, "X-Men", 2019)], db_path)
    assert it["metron_series_id"] == 55


def test_a_small_search_without_the_run_is_an_honest_miss(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    path = "/comic-books/reviews/dark-horse-comics/the-seeds"
    cc.enrich([_pick(path, "The Seeds", 2018)], db_path)
    m = Metron({2018: [{"id": 1, "title": "The Seeds", "publisher": "Dark Horse", "year": 2018}]},
               [{"id": 2, "title": "Star Trek: The Seeds of Salvation", "publisher": "IDW", "year": 2025}])
    cc.tick(path=db_path, client=m)
    assert all(c[2] is None for c in m.calls)                           # one page, not crowded: no year searches


def test_a_week_old_miss_is_asked_again(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    cc.enrich([_pick(XMEN, "X-Men", 2019)], db_path)
    cc.tick(path=db_path, client=Metron({}, []))
    with db._connect(db_path) as c:
        c.execute("UPDATE community_match SET checked_at = '2026-01-01 00:00:00'")
    cc.enrich([_pick(XMEN, "X-Men", 2019)], db_path)
    with db._connect(db_path) as c:
        assert c.execute("SELECT state FROM community_match").fetchone()[0] == "wanted"
