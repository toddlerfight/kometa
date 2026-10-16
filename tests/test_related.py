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
    monkeypatch.setattr(rel, "_resolved_lists", lambda path=None: [fake])          # the warm copy
    rel._nb_cache['value'] = None
    nb = rel._list_neighbours(db_path)
    assert set(nb[ids[5]]) == {ids[3], ids[4], ids[6], ids[7]}
    short = {"entries": fake["entries"][:5]}
    monkeypatch.setattr(rel, "_resolved_lists", lambda path=None: [short])
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


def test_warm_lists_skips_when_nothing_changed(db_path, monkeypatch):
    builds = []
    monkeypatch.setattr(rel, "_build_lists", lambda path: (builds.append(1), [])[1])
    monkeypatch.setattr(rel, "_warm_sig", {"path": None, "sig": None})
    rel._lists_cache.update(value=None); rel._nb_cache.update(value=None)
    rel.warm_lists(db_path); rel.warm_lists(db_path)
    assert builds == [1]                                                           # second warm-up: nothing to do
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    rel.warm_lists(db_path)
    assert builds == [1, 1]                                                        # a new run: rebuilt


def test_an_empty_answer_with_nothing_to_ask_is_retried_tomorrow_not_next_month(db_path):
    """4 Kids Walk Into A Bank: signals filled before its issues landed → an empty row
    cached for a month, nothing suggested. Now the empty row is dated to come back tomorrow,
    and the trickle asks again as soon as the run has credits or a Metron issue."""
    sid = db.add_series(title="4 Kids Walk Into A Bank", publisher="Black Mask", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_series_id = 15157 WHERE id = ?", (sid,))
    assert rel.fill_signals(sid, db_path, detail=lambda mid: {"credits": []}) is False       # nothing to ask yet
    sig = rel._signals(db_path)[sid]
    assert sig["creators"] == [] and rel._stale(sig["fetched_at"]) is False                   # not stale yet…
    with db._connect(db_path) as c:
        c.execute("UPDATE series_signals SET fetched_at = datetime(fetched_at, '-1 days', '-1 minutes') WHERE tracked_series_id = ?", (sid,))
    assert rel._stale(rel._signals(db_path)[sid]["fetched_at"]) is True                     # …but it is after a day
    # the issues land with Metron ids: the next trickle asks again at once, even though the row is fresh
    rel.fill_signals(sid, db_path, detail=lambda mid: {"credits": []})
    db.upsert_issue_status(sid, 1.0, "2016-01-01", 1, metron_issue_id=162437, path=db_path)
    asked = []
    import kometa.metron_client as mc
    orig = mc.issue_detail
    mc.issue_detail = lambda mid: (asked.append(mid) or {"credits": [{"role": "Writer", "name": "Matthew Rosenberg", "metron_creator_id": 7}], "arcs": []})
    try:
        assert rel.trickle_signals(path=db_path) == 1
    finally:
        mc.issue_detail = orig
    assert asked == [162437]
    assert rel._signals(db_path)[sid]["creators"][0]["name"] == "Matthew Rosenberg"


def test_trickle_covers_locg_only_runs(db_path):
    sid = db.add_series(title="Only On LOCG", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET locg_series_id = 999 WHERE id = ?", (sid,))
    assert rel.trickle_signals(path=db_path) == 1                 # it gets a row (empty, retried tomorrow) instead of never
    assert sid in rel._signals(db_path)


def test_role_weights_and_editorial_filtering(db_path):
    # Test _role_weight lookup directly
    assert rel._role_weight("writer") == 3.0
    assert rel._role_weight("story") == 3.0
    assert rel._role_weight("script") == 3.0
    assert rel._role_weight("plot") == 2.0
    assert rel._role_weight("artist") == 2.0
    assert rel._role_weight("penciller") == 2.0
    assert rel._role_weight("inker") == 1.0
    assert rel._role_weight("colorist") == 0.5
    assert rel._role_weight("letterer") == 0.25
    assert rel._role_weight("cover") == 0.0

    # Unhandled/editorial/production roles must strictly default to 0.0
    assert rel._role_weight("editor") == 0.0
    assert rel._role_weight("editor-in-chief") == 0.0
    assert rel._role_weight("designer") == 0.0
    assert rel._role_weight("executive producer") == 0.0
    assert rel._role_weight("cover artist") == 0.0
    assert rel._role_weight("production") == 0.0
    assert rel._role_weight(None) == 0.0
    assert rel._role_weight("some random credit") == 0.0

    # Compound roles take the maximum valid sub-role
    assert rel._role_weight("writer, artist") == 3.0
    assert rel._role_weight("artist, colorist") == 2.0
    assert rel._role_weight("editor, designer") == 0.0

    # Test related() integration: shared editor or designer must NOT relate
    s1 = _series(db_path, "Avengers", [
        ("writer", "Jonathan Hickman"),
        ("editor", "Tom Brevoort"),
        ("designer", "Idis Designer")
    ])
    s2 = _series(db_path, "Fantastic Four", [
        ("writer", "Jonathan Hickman"),
        ("editor", "Tom Brevoort")
    ])
    s3 = _series(db_path, "Spider-Man", [
        ("writer", "Dan Slott"),
        ("editor", "Tom Brevoort"),
        ("designer", "Idis Designer")
    ])

    recs = rel.related(s1, path=db_path)
    by_title = {r["title"]: r for r in recs}

    # Fantastic Four shares Jonathan Hickman (writer = 3.0)
    assert "Fantastic Four" in by_title
    assert by_title["Fantastic Four"]["score"] == 3.0
    assert by_title["Fantastic Four"]["why"] == ["Jonathan Hickman"]

    # Spider-Man shares ONLY editor (Tom Brevoort) and designer (Idis Designer) -> must NOT relate!
    assert "Spider-Man" not in by_title


def test_fill_signals_aggregates_credits_across_multiple_issues(db_path, monkeypatch):
    import kometa.record as record
    sid = db.add_series(title="Hawkeye", publisher="Marvel Comics", folder_path=None, on_pull_list=False, path=db_path)
    # Simulate issue_record having two issues with distinct creative teams
    fake_issues = [
        {
            "number": 1.0,
            "source": "metron",
            "credits": [
                {"role": "Writer", "name": "Matt Fraction", "metron_creator_id": 101},
                {"role": "Artist", "name": "David Aja", "metron_creator_id": 102}
            ],
            "arcs": ["My Life as a Weapon"]
        },
        {
            "number": 2.0,
            "source": "metron",
            "credits": [
                {"role": "Writer", "name": "Matt Fraction", "metron_creator_id": 101},
                {"role": "Artist", "name": "Javier Pulido", "metron_creator_id": 103},
                {"role": "Colorist", "name": "Matt Hollingsworth", "metron_creator_id": 104}
            ],
            "arcs": ["My Life as a Weapon", "The Tape"]
        }
    ]
    monkeypatch.setattr(record, "issues_for_series", lambda s_id, path=None: fake_issues if s_id == sid else [])

    assert rel.fill_signals(sid, db_path) is True
    sig = rel._signals(db_path)[sid]
    creators_by_name = {c["name"]: c for c in sig["creators"]}

    # All creators across both issues must be captured
    assert "Matt Fraction" in creators_by_name and creators_by_name["Matt Fraction"]["role"] == "writer"
    assert "David Aja" in creators_by_name and creators_by_name["David Aja"]["role"] == "artist"
    assert "Javier Pulido" in creators_by_name and creators_by_name["Javier Pulido"]["role"] == "artist"
    assert "Matt Hollingsworth" in creators_by_name and creators_by_name["Matt Hollingsworth"]["role"] == "colorist"
    assert set(sig["arcs"]) == {"My Life as a Weapon", "The Tape"}


def test_publisher_bound_franchise_stem_fallback(db_path):
    # DC Batman series with 0 creators and 0 arcs (variant/collection)
    b_orphan = db.add_series(title="Batman - Year One (Deluxe B&W)", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    rel.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO series_signals (tracked_series_id, creators_json, arcs_json) VALUES (?, '[]', '[]')", (b_orphan,))

    # Another DC Batman series
    b_other = _series(db_path, "Batman: The Devastator", [("writer", "Frank Tieri")])
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET publisher = 'DC Comics' WHERE id = ?", (b_other,))

    # An Image Batman parody (different publisher) -> must NOT match
    b_image = _series(db_path, "Batman Parody", [("writer", "Indie Creator")])
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET publisher = 'Image Comics' WHERE id = ?", (b_image,))

    # An unrelated DC series
    dc_other = _series(db_path, "Flash", [("writer", "Joshua Williamson")])
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET publisher = 'DC Comics' WHERE id = ?", (dc_other,))

    # b_orphan has no creator signals -> fallback to publisher-bound stem
    recs = rel.related(b_orphan, path=db_path)
    by_title = {r["title"]: r for r in recs}

    assert "Batman: The Devastator" in by_title
    assert by_title["Batman: The Devastator"]["score"] == 1.5
    assert by_title["Batman: The Devastator"]["why"] == ["Franchise: Batman"]

    # Different publisher must not match
    assert "Batman Parody" not in by_title
    # Different stem must not match
    assert "Flash" not in by_title

    # Series that DOES have creator overlap must NOT use stem fallback
    b_creator = _series(db_path, "Batman: City of Bane", [("writer", "Frank Tieri")])
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET publisher = 'DC Comics' WHERE id = ?", (b_creator,))

    recs_creator = rel.related(b_creator, path=db_path)
    by_creator = {r["title"]: r for r in recs_creator}
    assert "Batman: The Devastator" in by_creator
    # Recommends via Frank Tieri (score = 3.0), NOT stem fallback
    assert by_creator["Batman: The Devastator"]["score"] == 3.0
    assert by_creator["Batman: The Devastator"]["why"] == ["Frank Tieri"]


def test_fill_signals_filters_guest_creators_below_twenty_percent_presence(db_path, monkeypatch):
    import kometa.record as record
    sid = db.add_series(title="The Department of Truth", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)

    # 10-issue run: James Tynion IV writes all 10 issues, Martin Simmonds draws all 10 issues.
    # Guest writer Scott Snyder co-writes issue 1 only (1/10 = 10% < 20%).
    fake_issues = []
    for num in range(1, 11):
        credits = [
            {"role": "Writer", "name": "James Tynion IV", "metron_creator_id": 10},
            {"role": "Artist", "name": "Martin Simmonds", "metron_creator_id": 20},
        ]
        if num == 1:
            credits.append({"role": "Writer", "name": "Scott Snyder", "metron_creator_id": 30})
        fake_issues.append({"number": float(num), "credits": credits, "arcs": []})

    monkeypatch.setattr(record, "issues_for_series", lambda s_id, path=None: fake_issues if s_id == sid else [])

    assert rel.fill_signals(sid, db_path) is True
    sig = rel._signals(db_path)[sid]
    creators_by_name = {c["name"]: c for c in sig["creators"]}

    # Regular writer and artist (10/10 = 100% presence) must be kept
    assert "James Tynion IV" in creators_by_name and creators_by_name["James Tynion IV"]["role"] == "writer"
    assert "Martin Simmonds" in creators_by_name and creators_by_name["Martin Simmonds"]["role"] == "artist"

    # Guest writer (1/10 = 10% presence < 20% threshold) must be stripped
    assert "Scott Snyder" not in creators_by_name


