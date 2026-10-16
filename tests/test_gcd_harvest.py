"""The GCD harvest (enrichment spec 1g): parsing GCD's shapes, the conservative
series match, the fill, Metron taking a GCD row back, LOCG linking without a
Metron match. No network: requests and clients are stubbed throughout."""
import json

import pytest

import kometa.db as db
import kometa.record as rec
from kometa import gcd_client as gc, harvest


# --- shapes as GCD returns them (2026-10-12) --------------------------------------
SERIES = {
    "api_url": "https://www.comics.org/api/series/97435/?format=json",
    "name": "The Adventures of Tintin", "country": "us", "language": "en",
    "active_issues": ["https://www.comics.org/api/issue/550394/?format=json",
                      "https://www.comics.org/api/issue/550395/?format=json",
                      "https://www.comics.org/api/issue/550396/?format=json"],
    "issue_descriptors": ["368 - King Ottokar's Sceptre", "369", "[nn]"],
    "publishing_format": "was ongoing series", "year_began": 1959, "year_ended": 1962,
    "publisher": "https://www.comics.org/api/publisher/54/?format=json",
}
ISSUE = {
    "api_url": "https://www.comics.org/api/issue/550394/?format=json",
    "series_name": "The Adventures of Tintin (1959 series)", "descriptor": "368 - King Ottokar's Sceptre",
    "number": "368", "title": "King Ottokar's Sceptre", "publication_date": "1959",
    "key_date": "1959-00-00", "on_sale_date": "", "price": "1.95 USD; 2.50 CAD", "page_count": "64.000",
    "editing": "Mark Chiarello (editor); Paul Levitz (publisher)", "indicia_publisher": "Golden Press",
    "brand_emblem": "Golden Press", "isbn": "", "barcode": "0761941200019", "rating": "",
    "story_set": [
        {"type": "cover", "title": "", "sequence_number": 0, "script": "", "pencils": "Hergé [as Herge]",
         "inks": "Hergé", "colors": "?", "letters": "", "editing": "", "genre": "", "characters": "Tintin; Snowy",
         "synopsis": "Cover synopsis"},
        {"type": "comic story", "title": "King Ottokar's Sceptre", "sequence_number": 1,
         "script": "Hergé; Bob de Moor (plot)", "pencils": "Hergé", "inks": "Hergé ?", "colors": "Studios Hergé",
         "letters": "none", "editing": "", "genre": "adventure; humor",
         "characters": "Tintin; Snowy [Milou]; Villains: Müsstler (cameo); Captain Haddock",
         "synopsis": "Tintin uncovers a plot against King Muskar XII."},
    ],
    "cover": "https://files1.comics.org//img/gcd/covers_by_id/550/w400/550394.jpg",
}


def test_issue_detail_parses_credits_dates_and_cast():
    d = gc.parse_issue(ISSUE, 550394)
    pairs = {(c["role"], c["name"]) for c in d["credits"]}
    assert ("Writer", "Hergé") in pairs and ("Plot", "Bob de Moor") in pairs
    assert ("Penciller", "Hergé") in pairs and ("Inker", "Hergé") in pairs     # 'Hergé ?' — the doubt dropped
    assert ("Colorist", "Studios Hergé") in pairs
    assert ("Cover", "Hergé") in pairs                                          # [as Herge] stripped, deduped
    assert ("Editor", "Mark Chiarello") in pairs
    assert not any(c["name"] == "Paul Levitz" for c in d["credits"])            # masthead, not a maker
    assert not any(c["name"].lower() in ("none", "?") for c in d["credits"])
    assert len(pairs) == len(d["credits"])                                      # no duplicates
    assert d["credits"][0]["role"] == "Writer"                                  # makers first
    assert d["desc"] == "Tintin uncovers a plot against King Muskar XII."     # cover synopsis isn't the story
    assert d["store_date"] == "1959-01-01"                                     # key_date 00 → 01
    assert d["page_count"] == 64 and d["price"] == "1.95 USD" and d["isbn"] is None
    assert d["covers"] == [{"id": "g550394", "name": "Cover A (Main)", "thumb": ISSUE["cover"], "large": ISSUE["cover"]}]
    x = d["extra"]
    assert x["gcd_id"] == 550394 and x["upc"] == "0761941200019" and x["brand"] == "Golden Press"
    assert x["characters"] == ["Tintin", "Snowy", "Müsstler", "Captain Haddock"]
    assert x["genre"] == ["adventure", "humor"]


def test_on_sale_date_wins_over_key_date():
    d = gc.parse_issue(dict(ISSUE, on_sale_date="1986-05-00", key_date="1986-06-00"), 1)
    assert d["store_date"] == "1986-05-01"


def test_descriptors_give_numbers_and_titles(monkeypatch):
    assert gc.parse_number("368 - King Ottokar's Sceptre") == 368.0
    assert gc.parse_number("1/2") == 0.5
    assert gc.parse_number("12.1") == 12.1
    assert gc.parse_number("[nn]") is None
    assert gc.parse_number("12a") is None
    monkeypatch.setattr(gc, "_get", lambda path, **p: SERIES)
    d = gc.series_detail(97435)
    assert d["gcd_id"] == 97435 and d["issues"] == 3 and d["language"] == "en"
    assert d["issues_list"] == [{"gcd_issue_id": 550394, "number": 368.0, "title": "King Ottokar's Sceptre"},
                                {"gcd_issue_id": 550395, "number": 369.0, "title": None},
                                {"gcd_issue_id": 550396, "number": None, "title": None}]


# --- the client's manners -----------------------------------------------------------
class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body, {}

    def json(self):
        return self._body


@pytest.fixture
def gcd_db(db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.set_config({"gcd_user": "me", "gcd_pass": "pw"}, db_path)
    monkeypatch.setattr(gc, "_state", {"last": 0.0, "day": "", "count": 0, "paused_until": 0.0})
    monkeypatch.setattr(gc, "MIN_INTERVAL_S", 0)
    return db_path


def test_search_reads_two_pages_at_most_and_counts_each_request(gcd_db, monkeypatch):
    calls = []

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        calls.append((url, dict(params)))
        assert params["format"] == "json" and auth == ("me", "pw") and headers["User-Agent"]
        return _Resp(200, {"count": 145, "next": "https://x/?page=2", "results": [SERIES]})

    monkeypatch.setattr(gc.requests, "get", fake_get)
    rows = gc.search_series("Adventures of Tintin")
    assert len(calls) == 2 and len(rows) == 2
    assert calls[0][0].endswith("series/name/Adventures%20of%20Tintin/")
    assert calls[1][1]["page"] == 2
    assert gc.budget()["used"] == 2


def test_daily_cap_and_refusal_stop_the_client(gcd_db, monkeypatch):
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(gc.GCDUnavailable):
        gc.issue_detail(1)
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("paused")))
    with pytest.raises(gc.GCDUnavailable):
        gc.issue_detail(1)                                     # paused: no second knock
    gc._state.update(paused_until=0.0, count=gc.GCD_DAILY_CAP)
    with pytest.raises(gc.GCDUnavailable, match="spent"):
        gc.issue_detail(1)


def test_a_404_is_nothing_not_a_refusal(gcd_db, monkeypatch):
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: _Resp(404))
    assert gc.issue_detail(5) is None


# --- linking -------------------------------------------------------------------------
def _cand(gid, name="The Adventures of Tintin", lang="en", issues=30, began=1959, ended=None, country="us"):
    return {"gcd_id": gid, "name": name, "language": lang, "issues": issues, "year_began": began,
            "year_ended": ended, "country": country, "publishing_format": ""}


def test_one_clear_candidate_links():
    s = {"title": "Dune- House Atreides", "folder_path": "/comics/Dune- House Atreides (2020)"}
    results = [_cand(1, "Dune: House Atreides", issues=12, began=2020, ended=2021),
               _cand(2, "Dune: House Harkonnen", issues=12, began=2023),
               _cand(3, "Dune: House Atreides", lang="es", issues=12, began=2021)]
    assert harvest.gcd_match(s, 11.0, results) == ("linked", 1, [])


def test_many_plausible_is_ambiguous_with_candidates():
    s = {"title": "The Adventures of Tintin", "folder_path": "/comics/The Adventures of Tintin"}
    results = [_cand(i, began=1959 + i) for i in range(1, 11)] + [_cand(99, "Tintin", issues=3)]
    outcome, gid, cands = harvest.gcd_match(s, 28.0, results)
    assert outcome == "ambiguous" and gid is None
    assert len(cands) == 8 and 99 not in [c["gcd_id"] for c in cands]
    assert set(cands[0]) == {"gcd_id", "name", "year_began", "year_ended", "issues", "language", "country",
                             "publishing_format", "publisher_id"}


def test_too_short_or_foreign_is_never_linked_but_offered():
    s = {"title": "The Adventures of Tintin", "folder_path": None}
    results = [_cand(1, lang="fr", issues=24), _cand(2, issues=5)]            # French, or too few issues for #28
    outcome, gid, cands = harvest.gcd_match(s, 28.0, results)
    assert outcome == "ambiguous" and gid is None and {c["gcd_id"] for c in cands} == {1, 2}
    assert cands[0]["language"] == "en"


def test_year_in_the_title_must_fit():
    s = {"title": "Tank Girl (1991)", "folder_path": None}
    assert harvest.gcd_match(s, 4.0, [_cand(5, "Tank Girl", issues=4, began=2014)])[0] == "ambiguous"
    assert harvest.gcd_match(s, 4.0, [_cand(5, "Tank Girl", issues=4, began=1991)]) == ("linked", 5, [])


def test_no_same_name_candidate_is_none():
    s = {"title": "Neverender", "folder_path": None}
    assert harvest.gcd_match(s, 3.0, [_cand(1, "Neverending Story")]) == ("none", None, [])
    assert harvest.gcd_match(s, 3.0, []) == ("none", None, [])


def test_queries_fix_folder_colons_and_drop_the_year_and_the():
    assert harvest._gcd_queries("The Adventures of Tintin (1959)") == ["Adventures of Tintin"]
    assert harvest._gcd_queries("Dune- House Atreides") == ["Dune: House Atreides", "Dune- House Atreides"]


class _FakeGCD:
    def __init__(self, search=None, series=None, issues=None):
        self.search, self.series, self.issues, self.calls = search or {}, series or {}, issues or {}, []

    def search_series(self, q, year=None):
        self.calls.append(("search", q))
        return self.search.get(q, [])

    def series_detail(self, gid):
        self.calls.append(("series", gid))
        return self.series.get(gid)

    def issue_detail(self, iid):
        self.calls.append(("issue", iid))
        return self.issues.get(iid)


def _owned(sid, numbers, path, metron=None):
    for n in numbers:
        db.upsert_issue_status(sid, float(n), None, 1, metron_issue_id=(metron or {}).get(n), path=path)


def test_link_tick_writes_outcomes_most_owned_first(gcd_db):
    a = db.add_series(title="Dune- House Atreides", publisher="BOOM!", on_pull_list=False, path=gcd_db)
    b = db.add_series(title="The Adventures of Tintin", publisher="Casterman", on_pull_list=False, path=gcd_db)
    c = db.add_series(title="Saga", publisher="Image", on_pull_list=False, path=gcd_db)
    d = db.add_series(title="Neverender", publisher="X", on_pull_list=False, path=gcd_db)
    _owned(a, range(1, 12), gcd_db)
    _owned(b, range(1, 29), gcd_db)
    _owned(c, range(1, 40), gcd_db)
    _owned(d, [1], gcd_db)
    db.set_metron_series_id(c, 7, gcd_db)                                     # Metron has it: not GCD's job
    fake = _FakeGCD(search={
        "Dune: House Atreides": [_cand(11, "Dune: House Atreides", issues=12, began=2020)],
        "Adventures of Tintin": [_cand(i) for i in range(20, 25)]})
    assert harvest.gcd_link_tick(limit=3, path=gcd_db, client=fake) == 1
    assert [q for _, q in fake.calls] == ["Adventures of Tintin", "Dune: House Atreides", "Neverender"]
    get = lambda sid: db.get_series_by_id(sid, gcd_db)
    assert (get(a)["gcd_link"], get(a)["gcd_series_id"]) == ("linked", 11)
    assert get(b)["gcd_link"] == "ambiguous" and len(json.loads(get(b)["gcd_candidates_json"])) == 5
    assert get(d)["gcd_link"] == "none" and get(c)["gcd_link"] is None
    assert harvest.gcd_link_tick(limit=3, path=gcd_db, client=fake) == 0     # asked once, remembered


def test_link_tick_stops_at_a_refusal_and_respects_the_toggle(gcd_db):
    sid = db.add_series(title="Tank Girl", publisher="Titan", on_pull_list=False, path=gcd_db)
    _owned(sid, [1], gcd_db)

    class Refusing(_FakeGCD):
        def search_series(self, q, year=None):
            raise gc.GCDUnavailable("refused")

    assert harvest.gcd_link_tick(path=gcd_db, client=Refusing()) == 0
    assert db.get_series_by_id(sid, gcd_db)["gcd_link"] is None              # unasked, not 'none'
    db.set_config({"gcd_enabled": "0"}, gcd_db)
    fake = _FakeGCD()
    assert harvest.gcd_link_tick(path=gcd_db, client=fake) == 0 and fake.calls == []


# --- filling -------------------------------------------------------------------------
def _detail(n):
    return {"desc": f"story {n}", "credits": [{"role": "Writer", "name": "Hergé", "people_id": None}],
            "covers": [], "store_date": "1959-01-01", "extra": {"gcd_id": n}, "source": "gcd"}


def test_fill_only_owned_metron_less_open_issues_within_budget(gcd_db):
    sid = db.add_series(title="Sin City", publisher="Dark Horse", on_pull_list=False, path=gcd_db)
    _owned(sid, [1, 2, 3, 4, 5], gcd_db, metron={5: 999})                    # #5 has a Metron id
    db.upsert_issue_status(sid, 6.0, None, 0, path=gcd_db)                   # not owned
    rec.write_issue(sid, 2.0, {"desc": "file tags"}, "comicinfo", "partial", {}, gcd_db)   # stopgap: GCD may fill
    rec.write_issue(sid, 3.0, {"desc": "LOCG said"}, "locg", "partial", {}, gcd_db)      # a catalogue: left alone
    db.set_gcd_link(sid, "linked", 50, path=gcd_db)
    fake = _FakeGCD(series={50: {"gcd_id": 50, "issues_list": [
        {"gcd_issue_id": 100 + n, "number": float(n), "title": None} for n in range(1, 7)]}},
        issues={100 + n: _detail(100 + n) for n in range(1, 7)})
    assert harvest.gcd_fill_tick(budget=2, path=gcd_db, client=fake) == 1      # the list + one issue
    assert fake.calls == [("series", 50), ("issue", 101)]
    fake.calls.clear()
    assert harvest.gcd_fill_tick(budget=12, path=gcd_db, client=fake) == 2     # list cached: #2, #4 only
    assert fake.calls == [("issue", 102), ("issue", 104)]
    for n in (1, 2, 4):
        row = rec.get_issue(sid, float(n), gcd_db)
        assert row["source"] == "gcd" and row["fill_state"] == "partial" and row["extra"]["gcd_id"] == 100 + n
    assert rec.get_issue(sid, 3.0, gcd_db)["source"] == "locg"
    assert rec.get_issue(sid, 5.0, gcd_db) is None and rec.get_issue(sid, 6.0, gcd_db) is None
    fake.calls.clear()
    assert harvest.gcd_fill_tick(path=gcd_db, client=fake) == 0 and fake.calls == []
    assert harvest.gcd_status(gcd_db)["filled"] == 3


def test_a_stopgap_never_overwrites_gcd_and_gcd_survives_a_refresh(gcd_db):
    from kometa import covers
    sid = db.add_series(title="Sin City", publisher="Dark Horse", on_pull_list=False, path=gcd_db)
    _owned(sid, [1], gcd_db)
    rec.write_issue(sid, 1.0, _detail(1), "gcd", "partial", {}, gcd_db)
    assert covers._record_is_open(sid, 1.0, gcd_db) is False                 # ComicInfo won't replace it
    row = rec.fill_issue(sid, 1.0, gcd_db, force=True)                       # no Metron id: GCD stands
    assert row["source"] == "gcd" and row["desc"] == "story 1"
    assert all(c["number"] != 1.0 for c in rec.pending_issues(gcd_db))


def test_metron_refills_a_gcd_row_once_it_has_an_id(gcd_db):
    sid = db.add_series(title="Sin City", publisher="Dark Horse", on_pull_list=False, path=gcd_db)
    _owned(sid, [1], gcd_db)
    rec.write_issue(sid, 1.0, _detail(1), "gcd", "partial", {}, gcd_db)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=4242, path=gcd_db)
    pend = [c for c in rec.pending_issues(gcd_db) if c["tracked_series_id"] == sid]
    assert [c["number"] for c in pend] == [1.0]
    metron = lambda issue, path: {"desc": "Metron's", "credits": [], "covers": [], "extra": {}}
    row = rec.fill_issue(sid, 1.0, gcd_db, issue=pend[0], metron=metron)
    assert row["source"] == "metron" and row["desc"] == "Metron's" and row["metron_issue_id"] == 4242


def test_progress_has_a_gcd_row(gcd_db, monkeypatch):
    monkeypatch.setattr(harvest, "DB_PATH", gcd_db)
    from kometa import metron_client, topup
    monkeypatch.setattr(metron_client, "budget", lambda: {})
    monkeypatch.setattr(topup, "status", lambda p: {})
    monkeypatch.setattr(harvest, "_images_pending", lambda p: 0)
    rec.ensure_tables(gcd_db)
    sid = db.add_series(title="X", publisher="Y", on_pull_list=False, path=gcd_db)
    db.set_gcd_link(sid, "ambiguous", candidates=[_cand(1)], path=gcd_db)
    g = harvest.progress(gcd_db)["gcd"]
    assert (g["linked"], g["ambiguous"], g["filled"]) == (0, 1, 0)


# --- picking a run by hand ------------------------------------------------------------
def test_patch_links_or_refuses_a_gcd_run(gcd_db, monkeypatch):
    from kometa import main
    monkeypatch.setattr(main, "DB_PATH", gcd_db)
    sid = db.add_series(title="Tintin", publisher="Casterman", on_pull_list=False, path=gcd_db)
    db.set_gcd_link(sid, "ambiguous", candidates=[_cand(1)], path=gcd_db)
    s = main.set_series_locg(sid, main.MatchRequest(gcd_id=97435))
    assert (s["gcd_link"], s["gcd_series_id"], s["gcd_candidates_json"]) == ("linked", 97435, None)
    assert s["match_status"] is None                                          # GCD's pick touches nothing else
    s = main.set_series_locg(sid, main.MatchRequest(gcd_id=0))
    assert (s["gcd_link"], s["gcd_series_id"]) == ("none", None)


# --- the schema ---------------------------------------------------------------------
def test_fresh_db_has_the_gcd_columns(db_path):
    with db._connect(db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
    assert {"gcd_series_id", "gcd_link", "gcd_candidates_json"} <= set(cols)


def test_old_db_rebuilds_then_keeps_the_gcd_columns(tmp_path):
    """A DB from before the nullable rebuilds: the GCD columns must survive them."""
    p = str(tmp_path / "old.db")
    import sqlite3
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE tracked_series (id INTEGER PRIMARY KEY AUTOINCREMENT,
        komga_series_id TEXT NOT NULL UNIQUE, metron_series_id INTEGER NOT NULL, title TEXT NOT NULL,
        publisher TEXT, year_began INTEGER, added_at TEXT, last_synced TEXT)""")
    conn.commit()
    conn.close()
    db.init_db(p)
    with db._connect(p) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_series)")]
    assert {"gcd_series_id", "gcd_link", "gcd_candidates_json"} <= set(cols)


# --- tie-breaks from what the shelf already says ------------------------------------
class _Pubs:
    def __init__(self, names):
        self.names, self.asked = names, []

    def publisher_name(self, pid):
        self.asked.append(pid)
        return self.names.get(pid)


def _cands():
    return [{"gcd_id": 1, "name": "Black Cat", "year_began": 2006, "year_ended": 2009, "language": "en", "publisher_id": 10},
            {"gcd_id": 2, "name": "Black Cat", "year_began": 2019, "year_ended": 2020, "language": "en", "publisher_id": 20},
            {"gcd_id": 3, "name": "Black Cat", "year_began": 2021, "year_ended": 2021, "language": "en", "publisher_id": 20}]


def test_publisher_folder_narrows_then_file_years_decide(db_path):
    from kometa import harvest
    import kometa.db as db
    sid = db.add_series(title="Black Cat", publisher="Marvel Comics", folder_path="/comics/Marvel Comics/Black Cat",
                        on_pull_list=False, path=db_path)
    db.upsert_book("/comics/Marvel Comics/Black Cat/Black Cat #001 (2019).cbz", 1, 1.0, [], tracked_series_id=sid, number=1.0, path=db_path)
    s = db.get_series_by_id(sid, db_path)
    pubs = _Pubs({10: "Aspen MLT", 20: "Marvel"})
    assert harvest.gcd_tiebreak(s, _cands(), db_path, pubs) == 2
    assert sorted(set(pubs.asked)) == [10, 20]


def test_still_ambiguous_means_no_link(db_path):
    from kometa import harvest
    import kometa.db as db
    sid = db.add_series(title="Black Cat", publisher="Marvel Comics", folder_path="/comics/Marvel Comics/Black Cat",
                        on_pull_list=False, path=db_path)
    s = db.get_series_by_id(sid, db_path)                                   # no files: no year to decide by
    assert harvest.gcd_tiebreak(s, _cands(), db_path, _Pubs({10: "Aspen MLT", 20: "Marvel"})) is None


def test_a_bogus_pack_year_cannot_force_a_wrong_link(db_path):
    from kometa import harvest
    import kometa.db as db
    sid = db.add_series(title="Black Cat", publisher="Marvel Comics", folder_path="/comics/Marvel Comics/Black Cat",
                        on_pull_list=False, path=db_path)
    db.upsert_book("/comics/Marvel Comics/Black Cat/Black Cat #001 (2015).cbz", 1, 1.0, [], tracked_series_id=sid, number=1.0, path=db_path)
    s = db.get_series_by_id(sid, db_path)
    assert harvest.gcd_tiebreak(s, _cands(), db_path, _Pubs({10: "Aspen MLT", 20: "Marvel"})) is None   # 2015 fits none: stays yours


# --- story genres per run (recommendations step 4) -----------------------------------
def test_gcd_genre_pairs_split_into_the_shelf_vocabulary():
    assert harvest.gcd_genre_labels(["superhero", "horror-suspense", "Detective-Mystery", "post-apocalyptic"]) == [
        "superhero", "horror", "suspense", "detective", "mystery", "post-apocalyptic"]


def test_gcd_genres_one_ask_per_run_and_none_when_the_record_already_says(db_path):
    db.set_config({"gcd_user": "me", "gcd_pass": "pw"}, db_path)
    asked = db.add_series(title="Asked", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    local = db.add_series(title="Already Said", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    for sid in (asked, local):
        db.upsert_issue_status(sid, 1.0, "2020-01-01", 1, path=db_path)
    rec.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO issue_record (tracked_series_id, number, source, fill_state, extra_json) VALUES (?, 1.0, 'metron', 'full', ?)",
                  (asked, json.dumps({"gcd_id": 501})))
        c.execute("INSERT INTO issue_record (tracked_series_id, number, source, fill_state, extra_json) VALUES (?, 1.0, 'gcd', 'full', ?)",
                  (local, json.dumps({"gcd_id": 777, "genre": ["fantasy"]})))
    calls = []
    class Fake:
        def issue_detail(self, gid):
            calls.append(gid)
            return {"extra": {"gcd_id": gid, "genre": ["superhero", "horror-suspense"]}}
    assert harvest.gcd_genre_tick(limit=5, path=db_path, client=Fake()) == 2
    assert calls == [501]                                                    # the GCD-filled record needed no request
    assert sorted(t["key"] for t in rec.tags_for(asked, db_path)) == ["horror", "superhero", "suspense"]
    assert [t["key"] for t in rec.tags_for(local, db_path)] == ["fantasy"]
    assert harvest.gcd_genre_tick(limit=5, path=db_path, client=Fake()) == 0 and calls == [501]   # asked once


def test_gcd_genres_sit_between_metron_and_the_files(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    rec.set_tags(sid, "gcd", ["science fiction", "suspense"], db_path)
    rec.set_tags(sid, "comicinfo", ["fantasy", "zombies"], db_path)
    eff = lambda: {(t["key"], t["source"], t["weight"]) for t in rec.effective_tags(sid, db_path)}
    assert eff() == {("science fiction", "gcd", rec.GCD_TRUST), ("suspense", "gcd", rec.GCD_TRUST),
                     ("fantasy", "comicinfo", rec.FILE_THEME_TRUST), ("zombies", "comicinfo", rec.FILE_THEME_TRUST)}
    rec.set_tags(sid, "metron", ["Fantasy", "Science Fiction"], db_path)    # Metron answers: its coarse genres rule
    assert eff() == {("fantasy", "metron", 1.0), ("science fiction", "metron", 1.0),
                     ("suspense", "gcd", rec.GCD_TRUST), ("zombies", "comicinfo", rec.FILE_THEME_TRUST)}
