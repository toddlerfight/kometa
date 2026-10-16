"""The polite extras (enrichment spec 1i): PRH's print covers, Open Library's
book ratings and covers, ComicBookRoundup's scores. Fully offline — the one
HTTP seam (polite.requests.get) is stubbed per test, and the pages here are
small hand-made stand-ins with the same structure, not copies of the sites."""
import hashlib
import io
import json

import pytest
from PIL import Image

import kometa.db as db
import kometa.record as rec
from kometa import cbr, harvest, images, main, openlibrary, polite, prh


# --- an HTTP stub: url → (status, body, headers) ----------------------------------
class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode() if not isinstance(body, str) else body.encode()
        self.text = self.content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content)

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def close(self):
        pass


@pytest.fixture
def web(monkeypatch):
    """routes[url] = (status, body[, headers]); every call is logged; an unknown
    URL fails the test (nothing leaves the box)."""
    routes, calls = {}, []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        assert headers and headers.get("User-Agent", "").startswith("Kometa/")
        if url not in routes:
            raise AssertionError(f"unexpected request: {url}")
        status, body, *h = routes[url]
        return _Resp(status, body, h[0] if h else None)

    monkeypatch.setattr(polite.requests, "get", fake_get)
    for lim in (prh.limiter, openlibrary.limiter, cbr.limiter):
        lim.reset()
        monkeypatch.setattr(lim, "min_interval_s", 0)
    monkeypatch.setattr(prh, "_placeholder", {"md5": None})
    return routes, calls


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "COVERS_DIR", str(tmp_path / "covers"))
    return tmp_path / "covers"


def _owned(db_path, title="Saga", publisher="Image Comics", n=1.0, pull=True, **issue):
    sid = db.add_series(title=title, publisher=publisher, folder_path=None, on_pull_list=pull, path=db_path)
    db.upsert_issue_status(sid, n, issue.get("store_date"), 1, metron_issue_id=issue.get("metron_issue_id", 10),
                           path=db_path)
    return sid


# --- the throttle ---------------------------------------------------------------------
def test_limiter_caps_the_day_and_rests_after_a_refusal(web):
    routes, calls = web
    lim = polite.Limiter("Test", min_interval_s=0, daily_cap=2)
    routes["https://x/a"] = (200, b"ok")
    routes["https://x/b"] = (429, b"", {"Retry-After": "120"})
    assert lim.get("https://x/a").status_code == 200
    with pytest.raises(polite.SourceUnavailable):
        lim.get("https://x/b")                                  # refused → resting
    assert lim.budget()["paused"]
    with pytest.raises(polite.SourceUnavailable):
        lim.get("https://x/a")                                  # resting: not even sent
    assert calls == ["https://x/a", "https://x/b"]
    lim.reset()
    lim.get("https://x/a")
    lim.get("https://x/a")
    with pytest.raises(polite.SourceUnavailable, match="spent"):
        lim.get("https://x/a")                                  # the cap counts what was sent


def test_limiter_keeps_its_gap(web, monkeypatch):
    routes, _ = web
    routes["https://x/a"] = (200, b"ok")
    lim = polite.Limiter("Test", min_interval_s=2.0, daily_cap=10)
    slept = []
    lim.sleep = slept.append
    lim.get("https://x/a")
    lim.get("https://x/a")
    assert len(slept) == 1 and 1.5 < slept[0] <= 2.0


def test_a_404_is_an_answer_not_an_outage(web):
    routes, _ = web
    routes["https://x/missing"] = (404, b"")
    lim = polite.Limiter("Test", min_interval_s=0, daily_cap=5)
    assert lim.get("https://x/missing").status_code == 404
    assert not lim.budget()["paused"]


# --- the record: other sources' scores beside LOCG's ---------------------------------
def test_community_sources_never_clobber_each_other(db_path):
    sid = _owned(db_path)
    rec.set_community(sid, 1.0, {"rating": 4.2, "pulls": 900}, db_path)
    locg_at = rec.get_issue(sid, 1.0, db_path)["community_at"]
    rec.merge_community(sid, 1.0, "cbr", {"critic": 8.7, "user": 8.5}, db_path)
    row = rec.get_issue(sid, 1.0, db_path)
    assert row["community"] == {"rating": 4.2, "pulls": 900, "cbr": {"critic": 8.7, "user": 8.5}}
    assert row["community_at"] == locg_at                     # LOCG's clock, untouched
    rec.set_community(sid, 1.0, {"rating": 4.4, "pulls": 950}, db_path)   # LOCG's refresh rewrites its half only
    assert rec.get_issue(sid, 1.0, db_path)["community"] == {"rating": 4.4, "pulls": 950,
                                                             "cbr": {"critic": 8.7, "user": 8.5}}


def test_a_new_row_from_another_source_still_owes_locg(db_path):
    sid = _owned(db_path)
    rec.merge_community(sid, 1.0, "cbr", {"critic": 9.0}, db_path)
    row = rec.get_issue(sid, 1.0, db_path)
    assert row["community"] == {"cbr": {"critic": 9.0}} and row["community_at"] is None
    assert row["fill_state"] == "variants"
    with pytest.raises(ValueError):
        rec.merge_community(sid, 1.0, "locg", {}, db_path)


def test_a_details_refresh_keeps_the_scores(db_path):
    sid = _owned(db_path)
    rec.set_community(sid, 1.0, {"rating": 4.2}, db_path)
    rec.merge_community(sid, 1.0, "cbr", {"critic": 8.0}, db_path)
    rec.write_issue(sid, 1.0, {"desc": "new", "extra": {}}, "metron", "full", {"metron_issue_id": 10}, db_path)
    row = rec.get_issue(sid, 1.0, db_path)
    assert row["desc"] == "new" and row["community"] == {"rating": 4.2, "cbr": {"critic": 8.0}}
    assert row["community_at"]


# --- PRH ----------------------------------------------------------------------------
PLACEHOLDER = b"\xff\xd8\xff\xe0 placeholder jpeg"
UPC = "75960620845100111"


def _tiff(w=120, h=180, mode="CMYK") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), (0, 128, 255, 0) if mode == "CMYK" else (10, 20, 30)).save(buf, "TIFF", compression="tiff_lzw")
    return buf.getvalue()


def _prh_issue(db_path, upc=UPC, n=1.0, isbn=None):
    sid = _owned(db_path, title="X-Orcists", publisher="Marvel", n=n)
    rec.write_issue(sid, n, {"desc": "d", "isbn": isbn, "extra": {"upc": upc}}, "metron", "full",
                    {"metron_issue_id": 10}, db_path)
    return sid


def test_prh_code_is_a_17_digit_upc_or_a_13_digit_isbn():
    assert prh.code_for({"upc": "75960620845100111"}, None) == UPC
    assert prh.code_for({"upc": "759606208451"}, None) is None              # no add-on: never invented
    assert prh.code_for({}, "978-1-5067-1234-5") == "9781506712345"
    assert prh.code_for({}, "1506712345") is None


def test_prh_stores_a_print_cover_as_jpeg_and_remembers_the_ask(db_path, web, store, monkeypatch):
    routes, calls = web
    monkeypatch.setattr(prh, "MIN_WIDTH", 100)
    sid = _prh_issue(db_path)
    routes[prh.BASE + prh.BOGUS_CODE] = (200, PLACEHOLDER)
    routes[prh.BASE + UPC] = (200, b"\xff\xd8\xff\xe0 a real small cover")
    routes[f"{prh.BASE}tif/{UPC}"] = (200, _tiff())
    assert prh.tick(path=db_path) == 1
    held = images.get(images.prh_key(sid, 1.0), db_path)
    assert held["source"] == "prh" and held["url"] == f"{prh.BASE}tif/{UPC}"
    data = open(held["path"], "rb").read()
    assert data[:3] == b"\xff\xd8\xff"                                     # a JPEG, never the TIFF
    im = Image.open(io.BytesIO(data))
    assert im.size == (120, 180) and im.mode == "RGB"                       # full resolution kept
    assert rec.get_issue(sid, 1.0, db_path)["extra"]["prh"] == f"{prh.BASE}tif/{UPC}"
    assert db.get_config(db_path)[prh.CONFIG_KEY] == hashlib.md5(PLACEHOLDER).hexdigest()
    calls.clear()
    assert prh.tick(path=db_path) == 0 and calls == []                       # asked once, never again


def test_prh_placeholder_costs_one_small_request_and_no_tiff(db_path, web, store):
    routes, calls = web
    sid = _prh_issue(db_path)
    db.set_config({prh.CONFIG_KEY: hashlib.md5(PLACEHOLDER).hexdigest()}, db_path)
    routes[prh.BASE + UPC] = (200, PLACEHOLDER)                             # PRH's 200 for 'not ours'
    assert prh.tick(path=db_path) == 0
    assert calls == [prh.BASE + UPC]
    assert rec.get_issue(sid, 1.0, db_path)["extra"]["prh"] is None
    assert images.get(images.prh_key(sid, 1.0), db_path) is None


def test_prh_too_small_isnt_kept(db_path, web, store):
    routes, _ = web
    sid = _prh_issue(db_path)
    db.set_config({prh.CONFIG_KEY: "something-else"}, db_path)
    routes[prh.BASE + UPC] = (200, b"real")
    routes[f"{prh.BASE}tif/{UPC}"] = (200, _tiff(60, 90, "RGB"))
    prh.tick(path=db_path)
    assert rec.get_issue(sid, 1.0, db_path)["extra"]["prh"] is None


def test_prh_off_or_refused_asks_nothing_more(db_path, web, store):
    routes, calls = web
    sid = _prh_issue(db_path)
    db.set_config({"prh_enabled": "0", prh.CONFIG_KEY: "x"}, db_path)
    assert prh.tick(path=db_path) == 0 and calls == []
    db.set_config({"prh_enabled": "1"}, db_path)
    routes[prh.BASE + UPC] = (403, b"")
    assert prh.tick(path=db_path) == 0
    assert "prh" not in rec.get_issue(sid, 1.0, db_path)["extra"]          # not recorded: asked again later
    assert prh.limiter.budget()["paused"]
    calls.clear()
    assert prh.tick(path=db_path) == 0 and calls == []


def test_prh_only_owned_issues(db_path, web, store):
    sid = _prh_issue(db_path)
    db.upsert_issue_status(sid, 1.0, None, 0, metron_issue_id=10, path=db_path)   # no longer owned
    assert prh.candidates(db_path) == []


# --- Open Library ---------------------------------------------------------------------
ISBN = "9781607066019"
EDITION = {"key": "/books/OL1M", "works": [{"key": "/works/OL2W"}], "covers": [-1, 4242],
           "identifiers": {"goodreads": ["15704307-saga-vol-1"]}, "number_of_pages": 160}
RATINGS = {"summary": {"average": 4.3529, "count": 68}, "counts": {"1": 1, "2": 2, "3": 5, "4": 20, "5": 40}}


def _ol_routes(routes):
    routes[f"{openlibrary.BASE}/isbn/{ISBN}.json"] = (200, EDITION)
    routes[f"{openlibrary.BASE}/works/OL2W/ratings.json"] = (200, RATINGS)
    routes[f"{openlibrary.COVERS}4242.jpg"] = (200, b"\xff\xd8\xff big cover", {"Content-Type": "image/jpeg"})


def test_openlibrary_trade_isbn_gets_ratings_and_cover(db_path, web, store):
    routes, calls = web
    sid = _owned(db_path)
    rec.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO trade_record (tracked_series_id, key, title, isbn) VALUES (?, 'm1', 'Saga Vol 1', ?)",
                     (sid, "978-1-60706-601-9"))
    _ol_routes(routes)
    assert openlibrary.tick(path=db_path) == 1
    got = openlibrary.get(ISBN, db_path)
    assert got["state"] == "found" and got["rating"] == 4.35 and got["rating_count"] == 68
    assert got["goodreads_id"] == "15704307" and got["cover_id"] == 4242 and got["histogram"]["5"] == 40
    held = images.get(images.isbn_key(ISBN, "ol"), db_path)
    assert held["source"] == "openlibrary" and open(held["path"], "rb").read().startswith(b"\xff\xd8")
    assert len(calls) == 3
    calls.clear()
    assert openlibrary.tick(path=db_path) == 0 and calls == []              # answered: not asked again for months


def test_openlibrary_snapshot_lands_on_an_issue_with_that_isbn(db_path, web, store):
    routes, calls = web
    sid = _owned(db_path)
    rec.write_issue(sid, 1.0, {"desc": "d", "isbn": ISBN}, "metron", "full", {"metron_issue_id": 10}, db_path)
    rec.set_community(sid, 1.0, {"rating": 4.0}, db_path)
    _ol_routes(routes)
    openlibrary.tick(path=db_path)
    c = rec.get_issue(sid, 1.0, db_path)["community"]
    assert c["rating"] == 4.0                                               # LOCG's, kept
    assert c["openlibrary"]["rating"] == 4.35 and c["openlibrary"]["count"] == 68
    assert c["openlibrary"]["source"] == "openlibrary" and c["openlibrary"]["at"]


def test_openlibrary_recheck_skips_a_cover_it_already_holds(db_path, web, store, monkeypatch):
    routes, calls = web
    sid = _owned(db_path)
    rec.write_issue(sid, 1.0, {"desc": "d", "isbn": ISBN}, "metron", "full", {"metron_issue_id": 10}, db_path)
    _ol_routes(routes)
    openlibrary.tick(path=db_path)
    monkeypatch.setattr(openlibrary, "RECHECK_DAYS", -1)                    # everything is due again
    calls.clear()
    openlibrary.tick(path=db_path)
    assert f"{openlibrary.COVERS}4242.jpg" not in calls and len(calls) == 2


def test_openlibrary_unknown_isbn_is_remembered_as_none(db_path, web, store):
    routes, calls = web
    sid = _owned(db_path)
    rec.write_issue(sid, 1.0, {"desc": "d", "isbn": ISBN}, "metron", "full", {"metron_issue_id": 10}, db_path)
    routes[f"{openlibrary.BASE}/isbn/{ISBN}.json"] = (404, b"")
    assert openlibrary.tick(path=db_path) == 1
    assert openlibrary.get(ISBN, db_path)["state"] == "none"
    assert "openlibrary" not in (rec.get_issue(sid, 1.0, db_path)["community"] or {})


def test_openlibrary_toggle_off_asks_nothing(db_path, web, store):
    routes, calls = web
    sid = _owned(db_path)
    rec.write_issue(sid, 1.0, {"desc": "d", "isbn": ISBN}, "metron", "full", {"metron_issue_id": 10}, db_path)
    db.set_config({"openlibrary_enabled": "0"}, db_path)
    assert openlibrary.tick(path=db_path) == 0 and calls == []


def test_isbns_normalise():
    assert openlibrary.norm_isbn("978-1-60706-601-9") == ISBN
    assert openlibrary.norm_isbn("160706601x") == "160706601X"
    assert openlibrary.norm_isbn("75960620845100111") is None


# --- ComicBookRoundup -----------------------------------------------------------------
def _cbr_page(name="Saga", years="March 2012 - March 2025", pub="image-comics", slug="saga"):
    """A hand-made page in the series page's structure (shape probed 2026-10-12)."""
    def row(n, critic, cn, user, un):
        c_count = f'<div class="review-count">{cn} Reviews</div>' if cn else ""
        return f"""<tr><td class="ratingCell">
            <div class="critic-rating"><div class="review green"><span>{critic}</span></div>{c_count}</div>
            <div class="user-rating" style="display: none;"><div class="review user green"><span>{user}</span></div>
              <div class="review-count">{un} Reviews</div></div></td>
            <td><a href="/comic-books/reviews/{pub}/{slug}/{n}" title="{n} Reviews">{n}</a></td></tr>"""
    return f"""<html><body><div class="issue-subhead"><span><a href="/comic-books/reviews/{pub}">Pub</a></span>
        <span>{years}</span></div><h1>{name}</h1>
        <div class="review-section"><div><div class="review green"><span>8.8</span></div>
          <h2>Critic Rating</h2><div class="review-count">792 Reviews</div></div>
          <div><div class="review user green"><span>8.7</span></div><h2>User Rating</h2>
          <div class="review-count">1,934 Reviews</div></div></div>
        <table><tbody>{row(2, "N/A", 0, "8.1", 13)}{row(1, "9.3", 26, "9.3", 61)}
        <tr><td><a href="/comic-books/reviews/{pub}/{slug}/annual-1">Annual 1</a></td></tr></tbody></table></body></html>"""


def test_cbr_parses_the_series_page():
    p = cbr.parse_series_page(_cbr_page())
    assert p["name"] == "Saga" and p["start_year"] == 2012
    assert (p["critic"], p["critic_reviews"], p["user"], p["user_reviews"]) == (8.8, 792, 8.7, 1934)
    assert set(p["issues"]) == {1.0, 2.0}                                   # the annual isn't a number of ours
    assert p["issues"][1.0]["critic"] == 9.3 and p["issues"][1.0]["user_reviews"] == 61
    assert p["issues"][2.0]["critic"] is None and p["issues"][2.0]["critic_reviews"] == 0


def test_cbr_slugs():
    assert cbr.series_slug("The Amazing Spider-Man (2025)") == "amazing-spider-man"
    assert cbr.series_slug("Alien Vs. X-Men") == "alien-vs.-x-men"
    assert cbr.series_slug("Miles Morales: Spider-Man") == "miles-morales-spider-man"
    assert cbr.publisher_slug("Marvel") == "marvel-comics"
    assert cbr.publisher_slug("BOOM! Studios") == "boom-studios"
    assert cbr.publisher_slug("Oni Press") == "oni-press"


def test_cbr_links_conservatively_and_scores_beside_locg(db_path, web):
    routes, calls = web
    sid = db.add_series(title="Saga", publisher="Image", year_began=2012, folder_path=None,
                        on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    db.upsert_issue_status(sid, 3.0, None, 1, metron_issue_id=3, path=db_path)          # not on the page
    rec.set_community(sid, 1.0, {"rating": 4.5}, db_path)
    routes[cbr.BASE + "image-comics/saga-(2012)"] = (404, "")
    routes[cbr.BASE + "image-comics/saga"] = (200, _cbr_page())          # the bare-slug page: half the live links
    assert cbr.tick(path=db_path) == 1
    c = rec.get_issue(sid, 1.0, db_path)["community"]
    assert c["rating"] == 4.5
    assert c["cbr"]["critic"] == 9.3 and c["cbr"]["user"] == 9.3 and c["cbr"]["user_reviews"] == 61
    assert c["cbr"]["url"].endswith("/image-comics/saga/1") and c["cbr"]["source"] == "comicbookroundup"
    assert rec.get_issue(sid, 3.0, db_path) is None
    s = cbr.get_series(sid, db_path)
    assert s["state"] == "linked" and s["critic"] == 8.8 and s["issues"] == 2
    calls.clear()
    assert cbr.tick(path=db_path) == 0 and calls == []                       # fresh for a month


def test_cbr_wrong_start_year_or_name_is_no_match(db_path, web):
    routes, _ = web
    sid = db.add_series(title="Saga", publisher="Image", year_began=1998, folder_path=None,
                        on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    routes[cbr.BASE + "image-comics/saga-(1998)"] = (200, _cbr_page(name="Saga of the Swamp Thing"))
    routes[cbr.BASE + "image-comics/saga"] = (200, _cbr_page())             # the 2012 run: not ours
    assert cbr.tick(path=db_path) == 0
    assert cbr.get_series(sid, db_path)["state"] == "none"
    assert rec.get_issue(sid, 1.0, db_path) is None


def test_cbr_no_year_means_no_guess_and_no_request(db_path, web):
    _, calls = web
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    assert cbr.tick(path=db_path) == 0 and calls == []
    assert cbr.get_series(sid, db_path)["state"] == "unsure"


def test_cbr_toggle_off_asks_nothing(db_path, web):
    _, calls = web
    sid = db.add_series(title="Saga", publisher="Image", year_began=2012, folder_path=None,
                        on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    db.set_config({"cbr_enabled": "0"}, db_path)
    assert cbr.tick(path=db_path) == 0 and calls == []


# --- Settings and the harvest card ----------------------------------------------------
@pytest.fixture
def wired(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    root = tmp_path / "comics"
    root.mkdir()
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(root))
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 0})
    return dbp


def test_no_login_sources_never_nag(wired):
    db.set_config({"metron_token": "t", "cv_api_key": "k", "gcd_user": "u", "gcd_pass": "p",
                   "prowlarr_url": "http://p", "prowlarr_apikey": "k", "sab_url": "http://s", "sab_apikey": "k",
                   "qbit_url": "http://q", "qbit_user": "u", "hardcover_token": "h"}, wired)
    assert main.settings_attention() == []
    prh.limiter._state["paused_until"] = 9e18                               # even resting after a refusal
    try:
        assert main.settings_attention() == []
    finally:
        prh.limiter.reset()


def test_toggles_round_trip_through_config(wired):
    cfg = main.get_config()
    assert cfg["prh_enabled"] and cfg["openlibrary_enabled"] and cfg["cbr_enabled"]
    assert {"prh", "openlibrary", "cbr"} <= set(cfg["extra_budgets"])
    assert cfg["extra_budgets"]["prh"]["cap"] == 300
    cfg = main.update_config(main.ConfigRequest(prh_enabled="0", openlibrary_enabled="0", cbr_enabled="0"))
    assert not (cfg["prh_enabled"] or cfg["openlibrary_enabled"] or cfg["cbr_enabled"])


def test_harvest_progress_has_a_row_per_source(db_path):
    rec.ensure_tables(db_path)
    p = harvest.progress(db_path)
    assert p["prh"]["budget"]["cap"] == 300 and p["openlibrary"]["budget"]["cap"] == 2000
    assert p["cbr"]["budget"]["cap"] == 500
    assert {"found", "none"} <= set(p["prh"]) and {"rated", "covers"} <= set(p["openlibrary"])
    assert {"linked", "issues"} <= set(p["cbr"])


def test_removing_a_series_drops_its_cbr_link(db_path, web):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    cbr.tick(path=db_path)                                                  # 'unsure' row, no request
    db.remove_series(sid, db_path)
    assert cbr.get_series(sid, db_path) is None


def test_prh_coming_soon_card_is_a_placeholder_too(monkeypatch):
    """Matched by picture: a grey card that looks like the known one is a placeholder."""
    import io
    from PIL import Image
    from kometa import prh, coververify
    card = io.BytesIO(); Image.new("RGB", (297, 449), (235, 235, 235)).save(card, "JPEG")
    monkeypatch.setattr(prh, "KNOWN_PLACEHOLDER_DHASHES", (coververify.dhash(card.getvalue()),))
    assert prh._is_placeholder(card.getvalue(), "not-this-md5")
    assert not prh._is_placeholder(_stripes(), "not-this-md5")        # an actual picture isn't


def _stripes():
    import io
    from PIL import Image
    im = Image.new("L", (297, 449))
    px = im.load()
    for x in range(297):
        for y in range(449):
            px[x, y] = 255 if (x // 20 + y // 30) % 2 else 0
    b = io.BytesIO(); im.convert("RGB").save(b, "JPEG")
    return b.getvalue()


def test_prh_only_asks_upcs_of_publishers_it_distributes(db_path, monkeypatch):
    import kometa.db as db
    from kometa import prh, record
    for title, pub, upc in (("Absolute Batman", "DC Comics", "76194138584600111"), ("Midnight Spider-Man", "Marvel Comics", "75960621681900111")):
        sid = db.add_series(title=title, publisher=pub, folder_path=None, on_pull_list=False, path=db_path)
        db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
        record.write_issue(sid, 1.0, {"desc": "x", "extra": {"upc": upc}}, "metron", "full", {"metron_issue_id": 1}, db_path)
    assert [c["code"] for c in prh.candidates(db_path)] == ["75960621681900111"]


def test_limiter_count_and_rest_survive_a_restart(web, tmp_path, monkeypatch):
    routes, _ = web
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "k.db"))
    routes["https://x/a"] = (200, b"ok")
    routes["https://x/b"] = (429, b"", {"Retry-After": "600"})
    lim = polite.Limiter("Persist", min_interval_s=0, daily_cap=3)
    lim.get("https://x/a")
    with pytest.raises(polite.SourceUnavailable):
        lim.get("https://x/b")
    again = polite.Limiter("Persist", min_interval_s=0, daily_cap=3)      # a deploy: same name, fresh object
    b = again.budget()
    assert b["used"] == 2 and b["paused"]
    assert polite.Limiter("Other", min_interval_s=0, daily_cap=3).budget()["used"] == 0
