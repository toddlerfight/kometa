"""GCD's download as the first answer (kometa/gcd_local.py): the file shaped like the
website's JSON, the website only past the snapshot, and a tick's website share. The
fixture is a hand-made sliver of the real dump's schema (2026-10-16); no network."""
import sqlite3

import pytest

import kometa.db as db
from kometa import gcd_client as gc, gcd_local, gapfill

_SCHEMA = """
CREATE TABLE stddata_country (id INTEGER PRIMARY KEY, code TEXT, name TEXT);
CREATE TABLE stddata_language (id INTEGER PRIMARY KEY, code TEXT, name TEXT, native_name TEXT);
CREATE TABLE gcd_publisher (id INTEGER PRIMARY KEY, name TEXT, deleted INTEGER DEFAULT 0);
CREATE TABLE gcd_indicia_publisher (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE gcd_brand (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE gcd_series (id INTEGER PRIMARY KEY, name TEXT, sort_name TEXT, year_began INTEGER,
  year_ended INTEGER, publisher_id INTEGER, country_id INTEGER, language_id INTEGER, has_issue_title INTEGER DEFAULT 0,
  is_singleton INTEGER DEFAULT 0, color TEXT, dimensions TEXT, paper_stock TEXT, binding TEXT,
  publishing_format TEXT, notes TEXT, deleted INTEGER DEFAULT 0);
CREATE TABLE gcd_issue (id INTEGER PRIMARY KEY, number TEXT, volume TEXT DEFAULT '', series_id INTEGER,
  indicia_publisher_id INTEGER, publication_date TEXT, key_date TEXT, sort_code INTEGER, price TEXT,
  page_count NUMERIC, indicia_frequency TEXT DEFAULT '', editing TEXT DEFAULT '', notes TEXT DEFAULT '',
  deleted INTEGER DEFAULT 0, isbn TEXT DEFAULT '', variant_of_id INTEGER, variant_name TEXT DEFAULT '',
  barcode TEXT DEFAULT '', title TEXT DEFAULT '', on_sale_date TEXT DEFAULT '', rating TEXT DEFAULT '',
  display_volume_with_number INTEGER DEFAULT 0, volume_not_printed INTEGER DEFAULT 0);
CREATE TABLE gcd_issue_brand_emblem (id INTEGER PRIMARY KEY, issue_id INTEGER, brand_id INTEGER);
CREATE TABLE gcd_story_type (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE gcd_story (id INTEGER PRIMARY KEY, title TEXT DEFAULT '', feature TEXT DEFAULT '',
  sequence_number INTEGER, page_count NUMERIC, issue_id INTEGER, script TEXT DEFAULT '', pencils TEXT DEFAULT '',
  inks TEXT DEFAULT '', colors TEXT DEFAULT '', letters TEXT DEFAULT '', editing TEXT DEFAULT '',
  genre TEXT DEFAULT '', characters TEXT DEFAULT '', synopsis TEXT DEFAULT '', notes TEXT DEFAULT '',
  no_script INTEGER DEFAULT 0, no_pencils INTEGER DEFAULT 0, no_inks INTEGER DEFAULT 0,
  no_colors INTEGER DEFAULT 0, no_letters INTEGER DEFAULT 0, no_editing INTEGER DEFAULT 0,
  type_id INTEGER, job_number TEXT DEFAULT '', deleted INTEGER DEFAULT 0, first_line TEXT DEFAULT '');
CREATE TABLE gcd_creator (id INTEGER PRIMARY KEY, gcd_official_name TEXT);
CREATE TABLE gcd_creator_name_detail (id INTEGER PRIMARY KEY, name TEXT, creator_id INTEGER,
  is_official_name INTEGER);
CREATE TABLE gcd_story_credit (id INTEGER PRIMARY KEY, deleted INTEGER DEFAULT 0, is_credited INTEGER DEFAULT 0,
  is_signed INTEGER DEFAULT 0, uncertain INTEGER DEFAULT 0, signed_as TEXT DEFAULT '', credited_as TEXT DEFAULT '',
  credit_name TEXT DEFAULT '', creator_id INTEGER, credit_type_id INTEGER, story_id INTEGER,
  is_sourced INTEGER DEFAULT 0);
CREATE TABLE gcd_issue_credit (id INTEGER PRIMARY KEY, deleted INTEGER DEFAULT 0, is_credited INTEGER DEFAULT 0,
  uncertain INTEGER DEFAULT 0, credited_as TEXT DEFAULT '', credit_name TEXT DEFAULT '', creator_id INTEGER,
  credit_type_id INTEGER, issue_id INTEGER, is_sourced INTEGER DEFAULT 0);
CREATE TABLE gcd_character_name_detail (id INTEGER PRIMARY KEY, name TEXT, character_id INTEGER);
CREATE TABLE gcd_story_character (id INTEGER PRIMARY KEY, deleted INTEGER DEFAULT 0, notes TEXT DEFAULT '',
  character_id INTEGER, story_id INTEGER);
CREATE TABLE gcd_character_relation (id INTEGER PRIMARY KEY, from_character_id INTEGER, relation_type_id INTEGER,
  to_character_id INTEGER);
"""


def _dump(path):
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    c.executescript("""
    INSERT INTO stddata_country VALUES (1, 'us', 'United States');
    INSERT INTO stddata_language VALUES (1, 'en', 'English', 'English');
    INSERT INTO gcd_publisher (id, name) VALUES (54, 'DC');
    INSERT INTO gcd_indicia_publisher VALUES (7, 'DC Comics');
    INSERT INTO gcd_brand VALUES (3, 'DC [bullet]'), (4, '[Vertigo V]');
    INSERT INTO gcd_series (id, name, sort_name, year_began, year_ended, publisher_id, country_id, language_id)
      VALUES (6139, 'Batman: Dark Victory', 'Batman: Dark Victory', 1999, 2000, 54, 1, 1),
             (6140, 'Batman: Year One', 'Batman: Year One', 1987, 1987, 54, 1, 1),
             (6141, 'Old Gone Series', 'Old Gone Series', 1999, 1999, 54, 1, 1);
    UPDATE gcd_series SET deleted = 1 WHERE id = 6141;
    INSERT INTO gcd_issue (id, number, series_id, indicia_publisher_id, publication_date, key_date, sort_code,
                           price, page_count, editing, barcode, on_sale_date)
      VALUES (256114, '12', 6139, 7, 'November 2000', '2000-11-00', 12, '2.95 USD', 36, '', '761941220543', '2000-09-20'),
             (256115, '13', 6139, 7, 'December 2000', '2000-12-00', 13, '2.95 USD', 36, '', '', ''),
             (256116, '13', 6139, 7, 'December 2000', '2000-12-00', 14, '2.95 USD', 36, '', '', '');
    UPDATE gcd_issue SET variant_of_id = 256115, variant_name = 'Newsstand' WHERE id = 256116;
    INSERT INTO gcd_issue (id, number, series_id, sort_code, deleted) VALUES (256117, '14', 6139, 15, 1);
    INSERT INTO gcd_issue_brand_emblem VALUES (1, 256114, 4), (2, 256114, 3);
    INSERT INTO gcd_story_type VALUES (6, 'cover'), (19, 'comic story'), (14, 'illustration');
    INSERT INTO gcd_story (id, sequence_number, page_count, issue_id, type_id, no_script, no_letters, characters)
      VALUES (451464, 0, 1, 256114, 6, 1, 1, 'Two-Face');
    INSERT INTO gcd_story (id, title, sequence_number, page_count, issue_id, type_id, letters, genre, synopsis)
      VALUES (451465, 'Revenge', 1, 23, 256114, 19, '', 'superhero', 'Two-Face goes after the Falcones.');
    INSERT INTO gcd_story (id, sequence_number, issue_id, type_id, pencils, deleted)
      VALUES (451466, 2, 256114, 14, 'Gene Colan', 1);
    INSERT INTO gcd_creator VALUES (1, 'Tim Sale'), (2, 'Jeph Loeb'), (3, 'Gregory Wright'), (4, 'Mark Chiarello'),
                                   (5, 'Joe Caramagna');
    INSERT INTO gcd_creator_name_detail VALUES (11, 'Tim Sale', 1, 1), (12, 'Jeph Loeb', 2, 1),
      (13, 'Gregory Wright', 3, 1), (14, 'Mark Chiarello', 4, 1), (15, 'VC''s Joe Caramagna', 5, 0);
    INSERT INTO gcd_story_credit (story_id, credit_type_id, creator_id, credit_name, uncertain, is_credited)
      VALUES (451464, 2, 11, '', 0, 0), (451464, 3, 11, '', 0, 0), (451464, 4, 14, '', 0, 0),
             (451465, 1, 12, '', 0, 0), (451465, 2, 11, '', 0, 0), (451465, 3, 11, '', 1, 0),
             (451465, 4, 13, 'colors', 0, 0), (451465, 5, 15, '', 0, 1);
    INSERT INTO gcd_story_credit (story_id, credit_type_id, creator_id, deleted) VALUES (451465, 1, 14, 1);
    INSERT INTO gcd_issue_credit (issue_id, credit_type_id, creator_id, credit_name) VALUES (256114, 6, 14, 'editor');
    INSERT INTO gcd_character_name_detail VALUES (21, 'Batman', 101), (22, 'Bruce Wayne', 102), (23, 'Catwoman', 103);
    INSERT INTO gcd_story_character (character_id, story_id) VALUES (21, 451465), (22, 451465), (23, 451465);
    INSERT INTO gcd_character_relation (from_character_id, relation_type_id, to_character_id) VALUES (101, 2, 102);
    """)
    c.commit()
    c.close()


@pytest.fixture
def gcd_file(db_path, monkeypatch, tmp_path):
    """Kometa's test database with GCD's download beside it, and no website login."""
    monkeypatch.setattr(db, "DB_PATH", db_path)
    _dump(str(tmp_path / "gcd.db"))
    monkeypatch.setattr(gcd_local, "_seen", {"db": None, "path": None, "at": 0.0, "ok": False})
    monkeypatch.setattr(gc, "_state", {"last": 0.0, "day": "", "count": 0, "paused_until": 0.0})
    monkeypatch.setattr(gc, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the website")))
    return db_path


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body, {}

    def json(self):
        return self._body


def test_an_issue_off_the_file_reads_like_the_website(gcd_file):
    d = gc.issue_detail(256114)
    names = {(c["role"], c["name"]) for c in d["credits"]}
    assert {("Writer", "Jeph Loeb"), ("Penciller", "Tim Sale"), ("Inker", "Tim Sale"),
            ("Colorist", "Gregory Wright"), ("Letterer", "Joe Caramagna"), ("Editor", "Mark Chiarello"),
            ("Cover", "Tim Sale"), ("Cover", "Mark Chiarello")} == names     # a deleted credit and story stay out
    assert d["store_date"] == "2000-09-20" and d["cover_date"] == "2000-11-01"
    assert d["desc"] == "Two-Face goes after the Falcones." and d["page_count"] == 36
    assert d["covers"] == []                                    # GCD doesn't hand covers out
    assert d["extra"]["brand"] == "DC [bullet]; [Vertigo V]" and d["extra"]["upc"] == "761941220543"
    assert d["extra"]["characters"] == ["Two-Face", "Batman", "Catwoman"]   # Bruce Wayne folded into Batman


def test_the_website_shape_itself(gcd_file):
    raw = gc._get("issue/256114/")
    cover, story = raw["story_set"]
    assert cover["script"] == "None" and cover["letters"] == "None"          # the no_ flags, as the website says
    assert story["colors"] == "Gregory Wright (colors)" and story["inks"] == "Tim Sale ?"
    assert story["letters"] == "Joe Caramagna (credited as VC's Joe Caramagna)"
    assert story["characters"] == "Batman [Bruce Wayne]; Catwoman"
    assert raw["editing"] == "Mark Chiarello (editor)" and raw["page_count"] == "36.000"
    assert raw["series"].endswith("/series/6139/?format=json")


def test_past_the_snapshot_asks_the_website_and_inside_it_never(gcd_file, monkeypatch):
    assert gc.issue_detail(256117) is None                      # deleted in the snapshot: the website says 404 too
    assert gc.issue_detail(200000) is None                      # never existed below the top id
    db.set_config({"gcd_user": "me", "gcd_pass": "pw"}, gcd_file)
    asked = []
    monkeypatch.setattr(gc.requests, "get", lambda url, **k: asked.append(url) or _Resp(404))
    assert gc.issue_detail(999999) is None and len(asked) == 1 and asked[0].endswith("issue/999999/")


def test_a_ticks_website_share_stops_the_tick_not_the_file(gcd_file, monkeypatch):
    db.set_config({"gcd_user": "me", "gcd_pass": "pw"}, gcd_file)
    asked = []
    monkeypatch.setattr(gc.requests, "get", lambda url, **k: asked.append(url) or _Resp(404))
    with gc.website_share(1):
        gc.issue_detail(999998)
        assert gc.issue_detail(256114)["store_date"] == "2000-09-20"     # the file is never counted
        with pytest.raises(gc.GCDUnavailable, match="share"):
            gc.issue_detail(999999)
    assert len(asked) == 1


def test_series_and_search_off_the_file(gcd_file):
    s = gc.series_detail(6139)
    assert [(i["gcd_issue_id"], i["number"]) for i in s["issues_list"]] == \
        [(256114, 12.0), (256115, 13.0), (256116, 13.0)]       # the deleted #14 isn't listed
    assert gc._get("series/6139/")["issue_descriptors"] == ["12", "13", "13 [Newsstand]"]
    assert s["language"] == "en" and s["country"] == "us" and s["publisher_id"] == 54
    assert [r["gcd_id"] for r in gc.search_series("batman")] == [6139, 6140]
    assert [r["gcd_id"] for r in gc.search_series("Batman", 1987)] == [6140]
    assert gc.publisher_name(54) == "DC"


def test_a_search_the_file_cant_answer_goes_to_the_website(gcd_file, monkeypatch):
    db.set_config({"gcd_user": "me", "gcd_pass": "pw"}, gcd_file)
    asked = []
    monkeypatch.setattr(gc.requests, "get",
                        lambda url, **k: asked.append(url) or _Resp(200, {"next": None, "results": []}))
    assert gc.search_series("Old Gone Series") == [] and len(asked) == 1   # deleted in the file: maybe back since


def test_the_file_alone_turns_gcd_on_and_takes_a_big_bite(gcd_file):
    assert gc.configured() and gc.local()
    assert gc.tick_size(6, 500) == 500
    db.set_config({"gcd_enabled": "0"}, gcd_file)
    assert not gc.configured()


def test_no_file_is_the_website_as_before(db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(gcd_local, "_seen", {"db": None, "path": None, "at": 0.0, "ok": False})
    assert not gc.local() and gc.tick_size(6, 500) == 6 and not gc.configured()


def test_gap_fill_takes_the_files_dates_without_the_website(gcd_file, monkeypatch):
    rows = [{"sid": 1, "number": 12.0, "gcd": 256114, "owned": 0}]
    monkeypatch.setattr(gapfill, "gcd_wanted", lambda path, limit: [dict(r, limit=limit) for r in rows])
    monkeypatch.setattr(gapfill, "_ids", lambda r: (None, r["gcd"]))
    monkeypatch.setattr(gapfill, "_suspect_gcd_date", lambda r: False)
    seen = []
    monkeypatch.setattr(gapfill, "_write", lambda conn, r, src, **k: seen.append((r["limit"], k)) or set())
    out = gapfill.gcd_fill(gcd_file)
    assert out["asked"] == 1 and seen[0][0] == gapfill.GCD_FILE_PER_TICK
    assert seen[0][1]["store_date"] == "2000-09-20" and seen[0][1]["desc"].startswith("Two-Face")
