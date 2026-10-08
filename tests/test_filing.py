"""Collected editions → under the run they collect (kometa/filing.py), with the
publisher wiki parsed (kometa/fandom_client.py) from real wikitext shapes."""
import pytest

import kometa.db as db
import kometa.fandom_client as fc
import kometa.filing as fi


DC_WIKITEXT = """{{Collected Edition
| Title = Batman: Year 100 and Other Tales Deluxe Edition
| Year = 2015
| Month = 10
| ISBN = 978-1401258078
| IssueList =
This Hardcover collects stories from the following issues:
* {{c|Batman: Year 100 Vol 1 1}}
* {{c|Batman: Year 100 Vol 1 2}}
* {{c|Batman: Year 100 Vol 1 3}}
* {{c|Batman: Year 100 Vol 1 4}}
* {{c|Batman Chronicles #11}}: "Berlin Batman"
* {{c|Solo Vol 1 3}}: "Teenage Sidekick"
}}"""

MARVEL_WIKITEXT = """{{Marvel Database:Comic Template
| Mode                    = TPB
| Year                    = 2023
| ISBN                    = 978-1302951078
| ReprintOf1              = Amazing Spider-Man Vol 2 698
| ReprintOf2              = Superior Spider-Man Vol 1 1
| ReprintOf3              = Superior Spider-Man Vol 1 27.NOW
| ReprintOf4              = Superior Spider-Man Annual Vol 1 1
}}"""


class TestParse:
    def test_entries(self):
        assert fc.parse_entry("Batman: Year 100 Vol 1 1") == {"series": "Batman: Year 100", "volume": 1, "number": 1.0}
        assert fc.parse_entry("Batman Chronicles #11") == {"series": "Batman Chronicles", "volume": 1, "number": 11.0}
        assert fc.parse_entry("Superior Spider-Man Vol 1 27.NOW")["number"] == 27.0
        assert fc.parse_entry("DC Black Label") is None

    def test_dc_page(self):
        p = fc.parse_collection(DC_WIKITEXT, "dc")
        assert p["year"] == 2015 and p["isbn"] == "978-1401258078" and len(p["collects"]) == 6

    def test_marvel_page_needs_a_collected_mode(self):
        p = fc.parse_collection(MARVEL_WIKITEXT, "marvel")
        assert p["year"] == 2023 and [e["series"] for e in p["collects"]][:2] == ["Amazing Spider-Man", "Superior Spider-Man"]
        assert fc.parse_collection(MARVEL_WIKITEXT.replace("= TPB", "= Comic"), "marvel") is None

    def test_wiki_for_publisher(self):
        assert fc.wiki_for("DC Comics") == "dc" and fc.wiki_for("Marvel Comics") == "marvel"
        assert fc.wiki_for("DC Black Label") == "dc" and fc.wiki_for("Image Comics") is None


class TestPlan:
    def test_parent_is_the_run_collected_most(self):
        c = fc.parse_collection(DC_WIKITEXT, "dc")["collects"]
        assert fi.choose_parent(c) == ("Batman: Year 100", 4, 6)

    def test_rows_ready_no_parent_not_found(self, db_path, monkeypatch):
        monkeypatch.setattr(fi, "DB_PATH", db_path)
        run = db.add_series(title="Batman - Year 100", publisher="DC Comics", folder_path="/c/DC Comics/Batman - Year 100", on_pull_list=False, path=db_path)
        db.set_match_status(run, "auto", db_path)
        deluxe = db.add_series(title="Batman - Year 100 and Other Tales Deluxe Edition", publisher="DC Comics",
                               folder_path="/c/DC Comics/Deluxe", on_pull_list=False, path=db_path)
        orphan = db.add_series(title="Superior Spider-Man Omnibus", publisher="Marvel Comics", folder_path="/c/Marvel/SSM", on_pull_list=False, path=db_path)
        image = db.add_series(title="Low Compendium", publisher="Image Comics", folder_path="/c/Image/Low", on_pull_list=False, path=db_path)
        found = {deluxe: fc.parse_collection(DC_WIKITEXT, "dc") | {"url": "u", "page": "p"},
                 orphan: fc.parse_collection(MARVEL_WIKITEXT, "marvel") | {"url": "u", "page": "p"}}
        cands = [db.get_series_by_id(i, db_path) for i in (deluxe, orphan, image)]
        p = fi.plan(db_path, candidates=cands, do_lookup=lambda s, path: found.get(s["id"]))
        by = {r["id"]: r for r in p["rows"]}
        assert by[deluxe]["status"] == "ready" and by[deluxe]["parent_id"] == run
        assert by[deluxe]["collects_summary"] == "Batman: Year 100 #1–4 + 2 more" and by[deluxe]["year"] == 2015
        assert by[orphan]["status"] == "no_parent" and "Superior Spider-Man" in by[orphan]["why"]
        assert by[image]["status"] == "not_found" and "no wiki" in by[image]["why"]
        assert p["counts"] == {"ready": 1, "no_parent": 1, "not_found": 1}

    def test_lookup_is_cached_including_misses(self, db_path, monkeypatch):
        monkeypatch.setattr(fi, "DB_PATH", db_path)
        calls = []
        monkeypatch.setattr(fc, "lookup_collection", lambda t, p: (calls.append(t), None)[1])
        s = {"id": 1, "title": "Batman - Hush", "publisher": "DC Comics"}
        assert fi.lookup(s, db_path) is None
        assert fi.lookup(s, db_path) is None
        assert calls == ["Batman - Hush"]                       # a miss is remembered too
