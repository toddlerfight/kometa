"""Covers and a way in for community picks (kometa/community_covers.py). No network."""
import kometa.db as db
from kometa import community_covers as cc


class FakeMetron:
    class MetronUnavailable(Exception):
        pass

    def __init__(self, series, issues):
        self.series, self.issues, self.calls = series, issues, []

    def configured(self):
        return True

    def harvest_share_spent(self):
        return False

    def search_series(self, title):
        self.calls.append(("search", title))
        return self.series

    def _get(self, path, **params):
        self.calls.append((path, params))
        return {"results": self.issues}

    @staticmethod
    def _num(n):
        try:
            return float(n)
        except (TypeError, ValueError):
            return None


def _pick(name, year, path="/comic-books/reviews/dc-comics/x", pub="dc-comics"):
    return {"series_path": path, "name": name, "year": year, "publisher": pub}


def test_a_pick_already_on_the_shelf_links_at_once_and_reads_as_yours(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    sid = db.add_series(title="Mister Miracle", publisher="DC", year_began=2017, folder_path="/c/mm", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO books (path, tracked_series_id, number) VALUES ('/c/mm/1.cbz', ?, 1)", (sid,))
    [it] = cc.enrich([_pick("Mister Miracle", 2017, "/comic-books/reviews/dc-comics/mister-miracle-(2017)")], db_path)
    assert it["series_id"] == sid and it["owned"] is True and it["cover"] == f"/api/series/{sid}/thumbnail"


def test_the_rest_wait_for_metron_and_get_the_first_issue_cover(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    p = "/comic-books/reviews/image-comics/redneck-(2017)"
    [it] = cc.enrich([_pick("Redneck", 2017, p, "image-comics")], db_path)
    assert "cover" not in it                                              # nothing yet: filed as wanted, no call made
    m = FakeMetron([{"id": 9, "title": "Redneck", "publisher": "Image", "year": 2017},
                    {"id": 8, "title": "Redneck", "publisher": "Other", "year": 1990}],
                   [{"number": "2", "image": "https://static.metron.cloud/2.jpg"},
                    {"number": "1", "image": "https://static.metron.cloud/1.jpg"}])
    assert cc.tick(path=db_path, client=m) == 1
    [it] = cc.enrich([_pick("Redneck", 2017, p, "image-comics")], db_path)
    assert it["metron_series_id"] == 9 and it["cover"] == "https://static.metron.cloud/1.jpg" and it["owned"] is False
    assert cc.tick(path=db_path, client=m) == 0                           # asked once, not every tick


def test_no_confident_match_is_left_alone(db_path, monkeypatch):
    monkeypatch.setattr(cc, "DB_PATH", db_path)
    cc.enrich([_pick("Velvet", 2013, "/comic-books/reviews/image-comics/velvet-(2013)", "image-comics")], db_path)
    m = FakeMetron([{"id": 1, "title": "Velvet Underground", "publisher": "X", "year": 2013}], [])
    cc.tick(path=db_path, client=m)
    [it] = cc.enrich([_pick("Velvet", 2013, "/comic-books/reviews/image-comics/velvet-(2013)", "image-comics")], db_path)
    assert "cover" not in it                                              # a wrong cover is worse than none
