"""Related / Suggestions (kometa/related.py): scored from cached signals, no network."""
import json

import kometa.db as db
import kometa.related as rel


def _series(db_path, title, creators, arcs=()):
    sid = db.add_series(title=title, publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, ?)",
                  (sid, json.dumps([{"role": r, "name": n} for r, n in creators]), json.dumps(list(arcs))))
    return sid


def test_related_scores_creators_arcs_and_list_neighbours(db_path):
    saga = _series(db_path, "Saga", [("writer", "Brian K. Vaughan"), ("artist", "Fiona Staples")])
    paper = _series(db_path, "Paper Girls", [("writer", "Brian K. Vaughan"), ("artist", "Cliff Chiang")])
    ymlm = _series(db_path, "Y: The Last Man", [("writer", "Brian K. Vaughan"), ("artist", "Pia Guerra")], arcs=["Unmanned"])
    other = _series(db_path, "Monstress", [("writer", "Marjorie Liu"), ("artist", "Sana Takeda")])
    r = rel.related(saga, path=db_path, neighbours={saga: {other: 3.0}})
    by = {x["title"]: x for x in r}
    assert set(by) == {"Paper Girls", "Y: The Last Man", "Monstress"}
    assert by["Paper Girls"]["why"] == ["Brian K. Vaughan"] and by["Paper Girls"]["score"] == 3.0
    assert by["Monstress"]["why"] == ["on a reading list together"]
    assert "Y: The Last Man" in by and by["Y: The Last Man"]["score"] == 3.0


def test_suggestions_follow_recent_reading_and_put_unstarted_owned_first(db_path, tmp_path):
    saga = _series(db_path, "Saga", [("writer", "Brian K. Vaughan")])
    paper = _series(db_path, "Paper Girls", [("writer", "Brian K. Vaughan")])
    ymlm = _series(db_path, "Y: The Last Man", [("writer", "Brian K. Vaughan")])
    # a book of Saga read this week; a book of Y started long ago
    sh = db.upsert_shelf_series(str(tmp_path / "Saga"), "Saga", "Image", saga, 1, "2026-10-09T00:00:00Z", db_path)
    db.index_books([(str(tmp_path / "Saga" / "Saga #001.cbz"), 10, 1.0, 1.0, sh, saga),
                    (str(tmp_path / "Y" / "Y #001.cbz"), 10, 1.0, 1.0, sh, ymlm)], db_path)
    b1 = db.get_book_by_path(str(tmp_path / "Saga" / "Saga #001.cbz"), db_path)["id"]
    b2 = db.get_book_by_path(str(tmp_path / "Y" / "Y #001.cbz"), db_path)["id"]
    from datetime import datetime, timedelta
    db.set_progress("me", b1, 5, False, datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), db_path)
    db.set_progress("me", b2, 5, False, (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"), db_path)
    s = rel.suggestions(path=db_path)
    assert [x["title"] for x in s] == ["Paper Girls", "Y: The Last Man"]      # Y was started, so it ranks after
    assert s[0]["because"] == ["Saga"] and s[0]["why"] == ["Brian K. Vaughan"]


def test_fill_signals_uses_the_lowest_owned_issue(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-01-01", 0, metron_issue_id=11, path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-02-01", 1, metron_issue_id=12, path=db_path)
    asked = []
    def detail(mid):
        asked.append(mid); return {"credits": [{"role": "Writer", "name": "Brian K. Vaughan"}], "arcs": ["Chapter One"]}
    assert rel.fill_signals(sid, db_path, detail=detail)
    assert asked == [12]
    sig = rel._signals(db_path)[sid]
    assert sig["creators"] == [{"role": "writer", "name": "Brian K. Vaughan", "id": None}] and sig["arcs"] == ["Chapter One"]


def test_a_long_list_only_relates_true_neighbours(db_path, monkeypatch):
    import kometa.readlists as rl
    ids = [db.add_series(title=f"S{i}", publisher="X", folder_path=None, on_pull_list=False, path=db_path) for i in range(20)]
    fake = {"entries": [{"position": i + 1, "series_id": sid} for i, sid in enumerate(ids)]}
    monkeypatch.setattr(rl, "get_lists", lambda path=None: [{"id": 1}])
    monkeypatch.setattr(rl, "resolve", lambda lid, path=None: fake)
    nb = rel._list_neighbours(db_path)
    assert set(nb[ids[5]]) == {ids[3], ids[4], ids[6], ids[7]}
    short = {"entries": fake["entries"][:5]}
    monkeypatch.setattr(rl, "resolve", lambda lid, path=None: short)
    assert set(rel._list_neighbours(db_path)[ids[0]]) == set(ids[1:5])


def test_outward_finds_what_the_shelf_lacks_by_the_same_people(db_path):
    rel.ensure_tables(db_path)
    saga = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.set_metron_series_id(saga, 916, db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, '[]')",
                  (saga, json.dumps([{"role": "writer", "name": "Brian K. Vaughan", "id": 7}, {"role": "letterer", "name": "Fonografiks", "id": 9}])))
    def fetch(cid):
        assert cid == 7
        return ([{"series": {"id": 916, "name": "Saga (2012)", "year_began": 2012}, "image": "s.jpg"}] * 3
                + [{"series": {"id": 55, "name": "Paper Girls (2015)", "year_began": 2015}, "image": "p.jpg"}] * 5
                + [{"series": {"id": 56, "name": "We Stand On Guard (2015)", "year_began": 2015}, "image": None}] * 1)
    out = rel.outward([saga], path=db_path, fetch=fetch)
    assert [o["title"] for o in out] == ["Paper Girls"]            # Saga is owned; one-issue credits are noise
    assert out[0]["why"] == ["Brian K. Vaughan wrote it"] and out[0]["cover"] == "p.jpg"
    # cached: a second call doesn't fetch
    assert rel.outward([saga], path=db_path, fetch=lambda cid: (_ for _ in ()).throw(AssertionError("fetched twice")))[0]["title"] == "Paper Girls"
