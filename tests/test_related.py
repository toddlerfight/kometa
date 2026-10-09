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
    rel._nb_cache['value'] = None
    nb = rel._list_neighbours(db_path)
    assert set(nb[ids[5]]) == {ids[3], ids[4], ids[6], ids[7]}
    short = {"entries": fake["entries"][:5]}
    monkeypatch.setattr(rl, "resolve", lambda lid, path=None: short)
    rel._nb_cache['value'] = None
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
    assert out[0]["why"] == ["Credit: Brian K. Vaughan"] and out[0]["cover"] == "p.jpg"   # no detail fn: role unknown
    # cached: a second call doesn't fetch
    assert rel.outward([saga], path=db_path, fetch=lambda cid: (_ for _ in ()).throw(AssertionError("fetched twice")))[0]["title"] == "Paper Girls"


def test_because_rows_anchor_on_recent_reading_use_a_series_once_and_need_four(db_path, tmp_path):
    bkv = [("writer", "Brian K. Vaughan")]
    saga = _series(db_path, "Saga", bkv)
    others = [_series(db_path, t, bkv) for t in ("Paper Girls", "Y: The Last Man", "Ex Machina", "Runaways")]
    lem = [("writer", "Jeff Lemire")]
    sweet = _series(db_path, "Sweet Tooth", lem)
    _series(db_path, "Descender", lem)                      # Jeff Lemire has one neighbour: too few for a row
    sh = db.upsert_shelf_series(str(tmp_path / "x"), "x", "Image", saga, 1, "2026-10-09T00:00:00Z", db_path)
    db.index_books([(str(tmp_path / "x" / "Saga #001.cbz"), 10, 1.0, 1.0, sh, saga),
                    (str(tmp_path / "x" / "Sweet Tooth #001.cbz"), 10, 1.0, 1.0, sh, sweet)], db_path)
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for name, when in (("Saga #001.cbz", now), ("Sweet Tooth #001.cbz", now - timedelta(days=1))):
        b = db.get_book_by_path(str(tmp_path / "x" / name), db_path)["id"]
        db.set_progress("me", b, 5, False, when.strftime("%Y-%m-%dT%H:%M:%SZ"), db_path)
    rows, pending = rel.because_rows(path=db_path)
    assert [r["anchor"] for r in rows] == ["Saga"]
    assert sorted(x["title"] for x in rows[0]["items"]) == ["Ex Machina", "Paper Girls", "Runaways", "Y: The Last Man"]
    assert all(x["because"] == [] for x in rows[0]["items"])           # the row title is the reason
    rows, _ = rel.because_rows(path=db_path, exclude=[others[0]])      # a task row already shows Paper Girls
    assert rows == []                                                    # three left: under the bar, no row


def _series_ids(db_path, title, creators):
    """Like _series but the credits carry Metron creator ids: (role, name, id)."""
    sid = db.add_series(title=title, publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, ?, ?)",
                  (sid, json.dumps([{"role": r, "name": n, "id": i} for r, n, i in creators]), "[]"))
    return sid


def test_creator_rows_one_per_weighty_credit_shelf_first_then_catalogue(db_path):
    pope, villa = ("artist", "Paul Pope", 7), ("colorist", "José Villarrubia", 8)
    me = _series_ids(db_path, "Batman - Year 100", [("writer", "Paul Pope", 7), pope, villa])
    shelf = [_series_ids(db_path, t, [pope]) for t in ("100%", "Heavy Liquid")]
    _series_ids(db_path, "Mister X", [villa])
    assert rel._cached_works(7, db_path) is None                                  # creates the table
    with db._connect(db_path) as c:
        c.execute("INSERT INTO creator_works (creator_id, works_json) VALUES (7, ?)", (json.dumps([
            {"metron_series_id": 501, "title": "THB", "year": 1994, "count": 6, "cover": "https://m/thb.jpg"},
            {"metron_series_id": 502, "title": "Escapo", "year": 1996, "count": 2, "cover": None},
            {"metron_series_id": 503, "title": "Heavy Liquid", "year": 1999, "count": 5, "cover": None},   # on the shelf already
            {"metron_series_id": 504, "title": "One-off", "year": 2000, "count": 1, "cover": None}]),))     # a single issue isn't a run
    rows, pending = rel.creator_rows(me, path=db_path)
    assert not pending and [r["name"] for r in rows] == ["Paul Pope"]          # the colorist carries no weight
    items = rows[0]["items"]
    assert [x["title"] for x in items] == ["100%", "Heavy Liquid", "THB", "Escapo"]
    assert [x["kind"] for x in items] == ["owned", "owned", "catalogue", "catalogue"]
    # every card says the person's role on THAT comic, in the modal's credit format
    assert all(x["why"] == ["Artist: Paul Pope"] for x in items if x["kind"] == "owned")
    assert me not in [x.get("series_id") for x in items]
    rows, _ = rel.creator_rows(me, path=db_path, min_items=5)
    assert rows == []                                                             # under the bar, no row
    page = rel.creator_page(7, path=db_path)
    assert page["name"] == "Paul Pope" and [x["title"] for x in page["shelf"]] == ["Batman - Year 100", "100%", "Heavy Liquid"]
    assert [x["title"] for x in page["catalogue"]] == ["THB", "Escapo"]


def test_creator_rows_wait_on_the_catalogue_when_uncached(db_path, monkeypatch):
    me = _series_ids(db_path, "Saga", [("writer", "Brian K. Vaughan", 9)])
    for t in ("Paper Girls", "Y: The Last Man", "Ex Machina", "Runaways"):
        _series_ids(db_path, t, [("writer", "Brian K. Vaughan", 9)])
    monkeypatch.setattr(rel, "_fill_works_in_background", lambda ids, path: None)
    rows, pending = rel.creator_rows(me, path=db_path)
    assert pending and [x["title"] for x in rows[0]["items"]] == ["Paper Girls", "Y: The Last Man", "Ex Machina", "Runaways"]


def test_creator_works_learn_the_role_per_work_and_drop_cover_only_credits(db_path):
    rel.ensure_tables(db_path)
    def fetch(cid):
        return ([{"id": 100 + k, "series": {"id": 300, "name": "Batman: Year 100 (2006)", "year_began": 2006}, "image": "y.jpg"} for k in range(4)]
                + [{"id": 200 + k, "series": {"id": 400, "name": "Adventure Time (2012)", "year_began": 2012}, "image": "a.jpg"} for k in range(3)])
    def detail(iid):
        if iid >= 200:
            return {"credits": [{"role": "Cover", "name": "Paul Pope", "metron_creator_id": 77}]}
        return {"credits": [{"role": "Artist", "name": "Paul Pope", "metron_creator_id": 77}, {"role": "Writer", "name": "Paul Pope", "metron_creator_id": 77}]}
    works = rel.creator_works(77, db_path, fetch=fetch, detail=detail)
    by = {w["title"]: w for w in works}
    assert by["Batman: Year 100"]["roles"] == ["artist", "writer"] and not by["Batman: Year 100"]["cover_only"]
    assert by["Adventure Time"]["cover_only"]
    assert rel.work_label(by["Batman: Year 100"], "Paul Pope") == "Writer: Paul Pope"
    cat, _ = rel._creator_catalogue(77, db_path, {}, cached_only=False, name="Paul Pope")
    assert [c["title"] for c in cat] == ["Batman: Year 100"] and cat[0]["why"] == ["Writer: Paul Pope"]


def test_a_fill_metron_cut_short_is_partial_and_retried_soon(db_path):
    rel.ensure_tables(db_path)
    fetch = lambda cid: [{"id": 1, "series": {"id": 5, "name": "A (2020)", "year_began": 2020}, "image": None}] * 2
    def detail(iid):
        raise RuntimeError("429")
    works = rel.creator_works(9, db_path, fetch=fetch, detail=detail)
    assert works[0]["partial"] and works[0]["roles"] == []
    assert rel._cached_works(9, db_path) is not None                       # fresh partial: served for now
    with db._connect(db_path) as c:
        c.execute("UPDATE creator_works SET fetched_at = datetime('now', '-11 minutes') WHERE creator_id = 9")
    assert rel._cached_works(9, db_path) is None                           # eleven minutes on: ask again
