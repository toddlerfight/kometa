"""ComicBookRoundup's people — "readers like you" (kometa/cbr_people.py). Fully
offline: the parsers read trimmed copies of live pages (tests/fixtures/cbr_people,
probed 2026-10-13), the tick gets a fake fetch and a fake clock, and the scoring
runs on hand-made people."""
import os
from datetime import datetime

import pytest

import kometa.db as db
from kometa import cbr, cbr_people as cp, marks, polite, record
from kometa.scheduler import TZ

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "cbr_people")
NIGHT = datetime(2026, 10, 12, 23, 30, tzinfo=TZ)
DAY = datetime(2026, 10, 12, 14, 0, tzinfo=TZ)
R = "/comic-books/reviews/image-comics/"


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


@pytest.fixture(autouse=True)
def _quiet_limiter(monkeypatch):
    monkeypatch.setattr(polite, "_state_path", lambda: None)
    cp.limiter.reset()
    monkeypatch.setattr(cp.limiter, "sleep", lambda s: None)
    cp._cache.clear()


@pytest.fixture
def web():
    """routes[url] = (status, text); an unknown URL fails the test."""
    routes, calls = {}, []

    def get(url):
        calls.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected request: {url}")
        return _Resp(*routes[url])
    return routes, calls, get


def _run(db_path, title, seg, owned=True, seed=False, year=None):
    """A tracked run cbr.py linked to R+seg; owned (an owned issue) unless told not."""
    sid = db.add_series(title=title, publisher="Image", year_began=year, folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2020-01-01", 1 if owned else 0, path=db_path)
    cbr.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO cbr_series (tracked_series_id, state, url, name, at) VALUES (?, 'linked', ?, ?, '2026-10-12 00:00:00')",
                     (sid, f"https://comicbookroundup.com{R}{seg}", title))
    if seed:
        marks.set_mark("series", sid, favourite=True, path=db_path)
    return sid


def _cbr_counts(db_path, sid, seg, number, n):
    record.merge_community(sid, float(number), "cbr", {"critic_reviews": n, "user_reviews": 0,
                                                       "url": f"https://comicbookroundup.com{R}{seg}/{number}"}, db_path)


def _issue_html(critics=(), users=()):
    """A minimal issue page in the live shape: (slug, outlet, score) / (id, score)."""
    c = "".join(f'<li><div class="list-review"><div class="review green"><span>{s}</span></div></div><div>'
                f'<h3>{o} - <a href="/comic-books/reviewer/{slug}">{slug}</a> </h3><span class="date">Mar 05, 2012</span></div></li>'
                for slug, o, s in critics)
    u = "".join(f'<li><div class="list-review"><div class="review user green"><span>{s}</span></div></div>'
                f'<div class="user-review-details"><h3><a href="/user/profile/{uid}">u{uid}</a></h3>'
                f'<span class="date">Nov 18, 2014</span></div></li>' for uid, s in users)
    return f'<div id="reviews-critic"><ul>{c}</ul></div><div id="reviews-user"><ul id="reviews">{u}</ul></div>'


# --- parsers ----------------------------------------------------------------------------
def test_issue_page_critics_and_readers():
    p = cp.parse_issue_page(_fixture("issue_saga_1.html"))
    assert [(c["ref"], c["outlet"], c["score"]) for c in p["critics"]] == [
        ("ron-richards", "iFanboy", 10.0), ("lan-pitts", "Newsarama", 10.0), ("tony-g-man-guerrero", "Comic Vine", 10.0)]
    assert p["critics"][2]["name"] == "Tony 'G-Man' Guerrero"          # entities decoded; keyed on the slug anyway
    assert p["critics"][0]["date"] == "2012-03-05"
    # two scored readers; the third row is a 'read it' tick with no score — skipped
    assert [(u["ref"], u["score"]) for u in p["users"]] == [("910", 10.0), ("2258", 10.0)]
    assert cp.parse_issue_page("<html><h1>Not here</h1></html>") is None


def test_critic_page_skips_run_summaries_and_keeps_odd_numbers():
    p = cp.parse_profile_page(_fixture("critic_lan_pitts.html"))
    assert p["name"] == "Lan Pitts" and p["outlet"] == "Newsarama"
    got = [(s["series_path"].rsplit("/", 1)[1], s["number"], s["score"]) for s in p["scores"]]
    assert ("12-reasons-to-die", "1", 4.0) in got
    assert ("adventure-time-(2012)", "2013-spoooktacular-1", 8.0) in got
    # a run with two reviewed issues: the summary row (its link has no number) is skipped,
    # each nested issue counts once
    assert [g for g in got if g[0] == "adventures-of-superman-(2013)"] == [
        ("adventures-of-superman-(2013)", "5", 9.0), ("adventures-of-superman-(2013)", "45", 10.0)]
    assert len(got) == len({(g[0], g[1]) for g in got}) == 8
    assert p["scores"][0]["date"] == "2013-05-13" and p["scores"][0]["series_name"] == "12 Reasons To Die"


def test_reader_page_nested_runs():
    p = cp.parse_profile_page(_fixture("user_reviews_910.html"))
    assert p["name"] == "Nick" and p["outlet"] is None
    assert [(s["series_path"], s["number"], s["score"]) for s in p["scores"]] == [
        ("/comic-books/reviews/dynamite/a-game-of-thrones", "1", 7.0)] + [
        ("/comic-books/reviews/image-comics/alex-ada", str(n), sc) for n, sc in ((1, 9.5), (2, 9.5), (3, 9.5), (4, 10.0), (5, 10.0))]
    assert p["scores"][1]["date"] == "2015-02-05" and p["scores"][1]["series_name"] == "Alex + Ada"
    assert cp.parse_profile_page("<html><h1>x</h1></html>") is None


# --- the queue and the tick -------------------------------------------------------------
def test_pass_a_is_number_one_plus_most_reviewed_seeds_first(db_path):
    a = _run(db_path, "Alpha", "alpha")
    b = _run(db_path, "Beta", "beta", seed=True)
    for num, n in ((1, 50), (2, 3), (3, 40), (4, 0), (5, 7), (6, 9)):
        _cbr_counts(db_path, a, "alpha", num, n)
    pages = cp.issue_pages(db_path)
    assert pages[0] == (f"{R}beta/1", f"{R}beta", "1")                 # your run first, #1 even with no counts
    assert [p[0] for p in pages[1:]] == [f"{R}alpha/{n}" for n in ("1", "3", "6", "5")]
    assert b


def test_window_toggle_and_spent_day_send_nothing(db_path, web):
    _run(db_path, "Alpha", "alpha")
    routes, calls, get = web
    db.set_config({"cbr_people_hours": "23-7"}, db_path)                 # a window set by hand (default is all day)
    assert cp.tick(db_path, now=DAY, get=get) == 0
    assert cp.tick(db_path, now=datetime(2026, 10, 13, 7, 0, tzinfo=TZ), get=get) == 0     # 7 is the end, exclusive
    db.set_config({"cbr_people_enabled": "0"}, db_path)
    assert cp.tick(db_path, now=NIGHT, get=get) == 0
    db.set_config({"cbr_people_enabled": "1", "cbr_enabled": "0"}, db_path)            # the CBR source switch rules too
    assert cp.tick(db_path, now=NIGHT, get=get) == 0
    db.set_config({"cbr_enabled": "1"}, db_path)
    cp.limiter._state.update(day=datetime.now().date().isoformat(), count=cp.limiter.daily_cap)
    assert cp.tick(db_path, now=NIGHT, get=get) == 0
    assert calls == []
    cp.limiter.reset()
    routes["https://comicbookroundup.com" + R + "alpha/1"] = (200, _issue_html(users=[("7", 8)]))
    assert cp.tick(db_path, now=NIGHT, get=get) == 1 and len(calls) == 1      # one page a tick
    db.set_config({"cbr_people_hours": "13-15"}, db_path)                      # the configured window is honoured
    assert cp.tick(db_path, now=DAY, get=get) == 0 and len(calls) == 1        # nothing left to ask
    assert cp.limiter.min_interval_s == 60 and cp.limiter.daily_cap == 1440 and cp.limiter is not cbr.limiter


def test_harvest_order_issue_pages_then_profiles_by_overlap(db_path, web):
    _run(db_path, "Alpha", "alpha")
    _run(db_path, "Beta", "beta")
    _run(db_path, "Gamma", "gamma")
    routes, calls, get = web
    site = "https://comicbookroundup.com"
    routes[site + R + "alpha/1"] = (200, _issue_html(critics=[("kay", "Outlet", 9)], users=[("1", 9), ("2", 7)]))
    routes[site + R + "beta/1"] = (200, _issue_html(critics=[("kay", "Outlet", 8)], users=[("1", 9)]))
    routes[site + R + "gamma/1"] = (200, _issue_html(users=[("1", 6), ("3", 5)]))
    for _ in range(3):
        cp.tick(db_path, now=NIGHT, get=get)
    assert calls == [site + R + p for p in ("alpha/1", "beta/1", "gamma/1")]
    # user 1 scored three of our runs, the critic two; users 2 and 3 one each — never asked
    assert cp.next_job(db_path) == ("profile", "user:1")
    routes[site + "/user/reviews/1"] = (200, _fixture("user_reviews_910.html"))
    routes[site + "/comic-books/reviewer/kay"] = (404, "")
    cp.tick(db_path, now=NIGHT, get=get)
    cp.tick(db_path, now=NIGHT, get=get)
    assert calls[3:] == [site + "/user/reviews/1", site + "/comic-books/reviewer/kay"]
    assert cp.next_job(db_path) is None
    with db._connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM cbr_people_scores WHERE person = 'user:1'").fetchone()[0]
        names = dict(conn.execute("SELECT series_path, name FROM cbr_people_series").fetchall())
    assert n == 3 + 6 and names["/comic-books/reviews/image-comics/alex-ada"] == "Alex + Ada"
    rep = cp.report(db_path)
    assert rep["pass_a_issue_pages"] == {"done": 3, "left": 0, "gone": 0, "unreadable": 0}
    assert rep["pass_b_profiles"] == {"done": 2, "left": 0}
    assert rep["people"]["users"] == 3 and rep["people"]["critics"] == 1 and rep["scores"] == 3 + 4 + 6
    # thirty days on, the profiles are due again
    with db._connect(db_path) as conn:
        conn.execute("UPDATE cbr_people SET fetched_at = '2020-01-01 00:00:00' WHERE person = 'user:1'")
    assert cp.next_job(db_path) == ("profile", "user:1")
    # idempotent: the same page again writes no second row
    cp.record_issue_page(R + "alpha/1", R + "alpha", "1", cp.parse_issue_page(routes[site + R + "alpha/1"][1]), db_path)
    assert cp.report(db_path)["scores"] == 3 + 4 + 6


def test_unreadable_pages_are_left_and_a_streak_rests_the_worker(db_path, web):
    for seg in ("a", "b", "c", "d"):
        _run(db_path, seg.upper(), seg)
    routes, calls, get = web
    for seg in ("a", "b", "c", "d"):
        routes[f"https://comicbookroundup.com{R}{seg}/1"] = (200, "<html>a redesign</html>")
    cp.tick(db_path, now=NIGHT, get=get)
    cp.tick(db_path, now=NIGHT, get=get)                     # second try of the same page → left alone
    assert calls == [f"https://comicbookroundup.com{R}a/1"] * 2
    assert cp.report(db_path)["pass_a_issue_pages"]["unreadable"] == 1
    assert not cp.limiter.spent()
    cp.tick(db_path, now=NIGHT, get=get)                     # third unreadable in a row: the site changed
    assert cp.limiter.spent() and "resting" in cp.report(db_path)["last_error"]
    assert cp.tick(db_path, now=NIGHT, get=get) == 0 and len(calls) == 3


# --- scoring ----------------------------------------------------------------------------
def _people(db_path, scores: dict[str, dict[str, float]]):
    cp.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        for who, by in scores.items():
            cp._upsert_person(conn, who, who)
            for seg, s in by.items():
                cp._put_score(conn, who, R + seg, "1", s, "2019-04-01", "profile")


def _reading(db_path, sid, completed=0, minutes_ago=5):
    """A book of the run with progress: completed=0 and recent = On Deck's Continue row."""
    from datetime import timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._connect(db_path) as conn:
        bid = conn.execute("INSERT INTO books (path, tracked_series_id, number) VALUES (?, ?, 1)",
                           (f"/c/{sid}-{minutes_ago}.cbz", sid)).lastrowid
        conn.execute("INSERT INTO read_progress (reader_id, book_id, page, completed, updated_at) VALUES ('me', ?, 3, ?, ?)",
                     (bid, completed, at))


TWIN = {"saga": 10, "east-of-west": 10, "great-new-thing": 10, "owned-not-seed": 10, "shelf-thing-(2020)": 10,
        "not-owned-tracked": 9.5, "filler-a": 5, "filler-b": 5, "filler-c": 5}
FLAT = {"saga": 9, "east-of-west": 9, "filler-a": 9, "filler-b": 9, "nines-pick": 9.5}
PANNED_FAN = {"panned": 10, "saga": 6, "east-of-west": 6, "panned-fans-pick": 10, "filler-a": 5, "filler-b": 5}


def _world(db_path) -> dict:
    sids = {"saga": _run(db_path, "Saga", "saga", seed=True),
            "eow": _run(db_path, "East of West", "east-of-west", seed=True),
            "owned": _run(db_path, "Owned Not Seed", "owned-not-seed"),          # yours: a faint seed, never recommended
            "panned": _run(db_path, "Panned", "panned"),
            "tracked": _run(db_path, "Not Owned Tracked", "not-owned-tracked", owned=False)}
    marks.set_mark("series", sids["panned"], rating=1, path=db_path)            # a 1★ seed counts AGAINST its fans
    with db._connect(db_path) as conn:                                        # an untracked folder on the shelf
        conn.execute("INSERT INTO shelf_series (folder_path, title, book_count) VALUES ('/c/Image/Shelf Thing (2020)', 'Shelf Thing (2020)', 3)")
    _people(db_path, {
        "user:1": TWIN, "user:2": TWIN, "critic:c1": TWIN,
        "user:3": {"saga": 10, "east-of-west": 10, "lukewarm": 6, "filler-a": 5, "filler-b": 5},
        # three readers who give everything a 9 — they centre to ~0 and weigh nothing
        "user:91": FLAT, "user:92": FLAT, "user:93": FLAT,
        # agrees on one seed only: luck, not taste
        "user:4": {"saga": 10, "one-seed-pick": 10, "filler-a": 3},
        # three who adore the run you gave one star and shrug at your favourites
        "user:61": PANNED_FAN, "user:62": PANNED_FAN, "user:63": PANNED_FAN,
    })
    return sids


def test_readers_like_you(db_path, monkeypatch):
    _world(db_path)
    out = cp.community(db_path)
    got = {i["series_path"].rsplit("/", 1)[1]: i for i in out["items"]}
    assert list(got)[0] == "great-new-thing" and "not-owned-tracked" in got
    for gone in ("owned-not-seed", "shelf-thing-(2020)", "saga", "east-of-west", "panned",
                 "nines-pick", "one-seed-pick", "lukewarm", "panned-fans-pick"):
        assert gone not in got
    top = got["great-new-thing"]
    assert top["supporters"] == 3 and top["critics"] == 1 and top["users"] == 2
    # the starred runs lead the reasons; the merely-owned one trails
    assert sorted(top["because"][:2]) == ["East of West", "Saga"] and top["because"][2] == "Owned Not Seed"
    assert top["label"].startswith("Liked by 3 readers who rate")
    assert top["name"] == "Great New Thing" and top["publisher_name"] == "Image" and top["year"] == 2019
    assert top["url"] == "https://comicbookroundup.com" + R + "great-new-thing"
    assert out["seeds"] == 4 and out["people"] == 4          # nines, one-seed reader and the panned run's fans dropped
    # cached: a new score without a new fetch doesn't move the answer; the route answers the same
    _people(db_path, {"user:5": TWIN})
    assert cp.community(db_path) == out
    monkeypatch.setattr(cp, "DB_PATH", db_path)
    assert cp.get_community()["items"] == out["items"]


def test_seed_weights_tiers_and_ratings_update_live(db_path):
    starred = _run(db_path, "Starred", "starred", seed=True)
    rated = _run(db_path, "Rated High", "rated-high")
    panned = _run(db_path, "Read Then Panned", "read-then-panned")
    read = _run(db_path, "Just Read", "just-read")
    reading = _run(db_path, "On Deck", "on-deck")
    shelf = _run(db_path, "Just Owned", "just-owned")
    marks.set_mark("series", rated, rating=5, path=db_path)
    marks.set_mark("series", panned, rating=1, path=db_path)
    _reading(db_path, panned, completed=1)
    _reading(db_path, read, completed=1)
    _reading(db_path, reading)                                   # in progress → On Deck
    assert cp.currently_reading(db_path) == [reading]
    w = cp.seed_weights(db_path)
    assert w == {starred: cp.W_FAVOURITE, rated: cp.W_OWNED + 2 * cp.W_RATING,
                 panned: cp.W_READ - 2 * cp.W_RATING, read: cp.W_READ, reading: cp.W_ONDECK, shelf: cp.W_OWNED}
    assert cp.W_ONDECK > cp.W_FAVOURITE > cp.W_READ > cp.W_OWNED > 0
    marks.set_mark("series", rated, rating=2, path=db_path)      # re-rated: the weight follows at once
    assert cp.seed_weights(db_path)[rated] == cp.W_OWNED - cp.W_RATING


def test_because_one_run(db_path, monkeypatch):
    sids = _world(db_path)
    r = cp.because(sids["saga"], db_path)
    assert r["seed"] == {"series_id": sids["saga"], "title": "Saga", "series_path": R + "saga", "supporters": 5}
    got = [i["series_path"].rsplit("/", 1)[1] for i in r["items"]]
    assert got[0] == "great-new-thing" and "not-owned-tracked" in got
    for gone in ("owned-not-seed", "shelf-thing-(2020)", "east-of-west", "one-seed-pick", "panned-fans-pick"):
        assert gone not in got                                     # owned, or < 3 supporters, or its fans didn't like Saga
    assert r["items"][0]["because"] == ["Saga"]
    monkeypatch.setattr(cp, "DB_PATH", db_path)
    assert cp.get_because(series_id=sids["saga"], limit=1)["items"] == r["items"][:1]
    unlinked = db.add_series(title="No Link", publisher="Image", path=db_path)
    assert cp.because(unlinked, db_path)["items"] == []


def test_because_ondeck_rows(db_path, monkeypatch):
    sids = _world(db_path)
    lonely = _run(db_path, "Lonely", "lonely")                   # linked, but nobody harvested scored it
    bare = db.add_series(title="Never Linked", publisher="Image", path=db_path)
    _reading(db_path, sids["saga"], minutes_ago=30)
    _reading(db_path, lonely, minutes_ago=20)
    _reading(db_path, bare, minutes_ago=10)
    _reading(db_path, sids["eow"], minutes_ago=40)
    assert cp.currently_reading(db_path) == [bare, lonely, sids["saga"], sids["eow"]]
    rows = cp.because_ondeck(db_path)
    assert [r["seed"]["title"] for r in rows] == ["Saga", "East of West"]    # the two without people skipped
    assert all(r["items"] and r["seed"]["supporters"] >= 3 for r in rows)
    monkeypatch.setattr(cp, "DB_PATH", db_path)
    assert [r["seed"]["series_id"] for r in cp.get_because_ondeck()["rows"]] == [sids["saga"], sids["eow"]]


def test_a_page_that_always_500s_is_set_aside_after_three_tries(db_path):
    """2026-10-14: profile user:4644 answered 500 every tick from 05:09 — nothing was
    marked, so every tick asked for it again and the whole harvest stood still."""
    _run(db_path, "Alpha", "alpha")
    page = f"{R}alpha/1"

    def broken(url):
        raise polite.SourceUnavailable("ComicBookRoundup people answered 500")
    for _ in range(2):
        assert cp.tick(db_path, now=NIGHT, get=broken) == 0
        assert cp.next_job(db_path)[1] == page                       # still the one to ask
    assert cp.tick(db_path, now=NIGHT, get=broken) == 0
    assert cp.next_job(db_path) is None or cp.next_job(db_path)[1] != page   # set aside: the harvest moves on


def test_a_refusal_never_sets_a_page_aside(db_path):
    _run(db_path, "Alpha", "alpha")

    def refused(url):
        raise polite.SourceUnavailable("ComicBookRoundup people refused us (429)")
    for _ in range(5):
        cp.tick(db_path, now=NIGHT, get=refused)
    assert cp.next_job(db_path)[1] == f"{R}alpha/1"                  # their 'slow down', not the page's fault


def test_cbr_people_scores_is_without_rowid_and_tracks_scores_version(db_path):
    cp.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cbr_people_scores'").fetchone()[0]
        assert "WITHOUT ROWID" in schema.upper()
        # WITHOUT ROWID tables do not have rowid
        with pytest.raises(Exception, match="no such column: rowid"):
            conn.execute("SELECT rowid FROM cbr_people_scores")
        # index is dropped
        idx = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_cbr_people_scores_series'").fetchone()
        assert idx is None

    # Version tracking bumps on page record
    with db._connect(db_path) as conn:
        v0 = cp._get_state(conn, "scores_version")
    assert v0 is not None
    cp.record_issue_page("/issue-page", "/comic-books/reviews/image-comics/run-1", "1",
                         {"critics": [], "users": [{"ref": "1", "name": "U1", "score": 9.0, "date": "2026-10-17"}]},
                         path=db_path)
    with db._connect(db_path) as conn:
        v1 = cp._get_state(conn, "scores_version")
        assert v1 is not None and v1 != v0


def test_cbr_people_series_first_year_cached_and_read_by_recommend(db_path):
    cp.ensure_tables(db_path)
    # Profile record sets first_year
    cp.record_profile("user:1", {
        "name": "User 1", "outlet": None,
        "scores": [{"series_path": "/comic-books/reviews/image-comics/saga-(2012)",
                    "series_name": "Saga (2012)", "number": "1", "score": 9.5, "date": "2012-03-14"}]
    }, path=db_path)
    with db._connect(db_path) as conn:
        r = conn.execute("SELECT first_year FROM cbr_people_series WHERE series_path = '/comic-books/reviews/image-comics/saga-(2012)'").fetchone()
        assert r is not None and r[0] == 2012


def test_legacy_scores_table_and_series_index_migrates_cleanly(tmp_path):
    legacy_path = str(tmp_path / "legacy.db")
    with db._connect(legacy_path) as conn:
        conn.execute("""CREATE TABLE cbr_people_scores (
            person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL, score REAL NOT NULL,
            date TEXT, via TEXT, at TEXT, PRIMARY KEY (person, series_path, number))""")
        conn.execute("CREATE INDEX idx_cbr_people_scores_series ON cbr_people_scores(series_path)")
        conn.execute("INSERT INTO cbr_people_scores VALUES ('user:1', '/comic-books/reviews/image/alpha-(2020)', '1', 9.0, '2020-01-01', 'issue', '2026-10-17')")
        # Non-slug series: no year in URL, multiple score dates
        conn.execute("INSERT INTO cbr_people_scores VALUES ('user:1', '/comic-books/reviews/image/beta-ongoing', '2', 8.0, '2019-06-15', 'issue', '2026-10-17')")
        conn.execute("INSERT INTO cbr_people_scores VALUES ('user:2', '/comic-books/reviews/image/beta-ongoing', '1', 8.5, '2018-03-10', 'issue', '2026-10-17')")
        conn.execute("CREATE TABLE cbr_people_series (series_path TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO cbr_people_series VALUES ('/comic-books/reviews/image/alpha-(2020)', 'Alpha')")
        conn.execute("INSERT INTO cbr_people_series VALUES ('/comic-books/reviews/image/beta-ongoing', 'Beta Ongoing')")

    # Migration happens inside ensure_tables
    cp.ensure_tables(legacy_path)

    with db._connect(legacy_path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cbr_people_scores'").fetchone()[0]
        assert "WITHOUT ROWID" in schema.upper()
        # Rows preserved
        rows = conn.execute("SELECT person, series_path, score FROM cbr_people_scores ORDER BY person, series_path").fetchall()
        assert [tuple(r) for r in rows] == [
            ('user:1', '/comic-books/reviews/image/alpha-(2020)', 9.0),
            ('user:1', '/comic-books/reviews/image/beta-ongoing', 8.0),
            ('user:2', '/comic-books/reviews/image/beta-ongoing', 8.5),
        ]
        # Index dropped
        idx = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_cbr_people_scores_series'").fetchone()
        assert idx is None
        # first_year column added: slug year for alpha, MIN(score year) for beta-ongoing
        series_alpha = conn.execute("SELECT name, first_year FROM cbr_people_series WHERE series_path = '/comic-books/reviews/image/alpha-(2020)'").fetchone()
        assert tuple(series_alpha) == ('Alpha', 2020)
        series_beta = conn.execute("SELECT name, first_year FROM cbr_people_series WHERE series_path = '/comic-books/reviews/image/beta-ongoing'").fetchone()
        assert tuple(series_beta) == ('Beta Ongoing', 2018)


def test_migration_recovers_from_leftover_v2_table(tmp_path):
    crash_path = str(tmp_path / "crash.db")
    with db._connect(crash_path) as conn:
        conn.execute("""CREATE TABLE cbr_people_scores (
            person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL, score REAL NOT NULL,
            date TEXT, via TEXT, at TEXT, PRIMARY KEY (person, series_path, number))""")
        conn.execute("INSERT INTO cbr_people_scores VALUES ('critic:c1', '/comic-books/reviews/marvel/x-men', '1', 8.0, '2024-01-01', 'issue', '2026-10-17')")
        # Leftover v2 table from aborted/crashed previous run
        conn.execute("""CREATE TABLE cbr_people_scores_v2 (
            person TEXT NOT NULL, series_path TEXT NOT NULL, number TEXT NOT NULL, score REAL NOT NULL,
            date TEXT, via TEXT, at TEXT, PRIMARY KEY (person, series_path, number)) WITHOUT ROWID""")

    # Should safely drop the leftover v2 and migrate successfully without 'table already exists' error
    cp.ensure_tables(crash_path)

    with db._connect(crash_path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cbr_people_scores'").fetchone()[0]
        assert "WITHOUT ROWID" in schema.upper()
        row = conn.execute("SELECT person, series_path, score FROM cbr_people_scores").fetchone()
        assert tuple(row) == ('critic:c1', '/comic-books/reviews/marvel/x-men', 8.0)
        # v2 table does not exist
        v2 = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cbr_people_scores_v2'").fetchone()
        assert v2 is None


