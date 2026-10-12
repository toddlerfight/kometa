"""Clean ComicInfo from the record (kometa/comicinfo.py, enrichment 3a)."""
import xml.etree.ElementTree as ET

from kometa import comicinfo as ci

SERIES = {"title": "Midnight Spider-Man", "publisher": "Marvel Comics", "year_began": 2026}
ROW = {
    "number": 1.0, "source": "metron", "desc": "A young Peter Parker…", "cover_date": "2026-12-01", "store_date": "2026-10-07",
    "metron_issue_id": 181715, "cv_issue_id": "1196213", "locg_issue_id": "3951157",
    "credits": [{"role": "Writer", "name": "Phillip Kennedy Johnson"}, {"role": "Artist", "name": "ScieTronc"},
                {"role": "Cover", "name": "Steve Beach"}, {"role": "Editor-in-Chief", "name": "C.B. Cebulski"},
                {"role": "Penciller, Inker", "name": "Someone Else"}, {"role": "Cover Colorist", "name": "Ryan Stegman"}],
    "arcs": ["Birth"], "community": {"rating": 4.2, "pulls": 23355},
    "extra": {"cv_id": 1196213, "gcd_id": 2870856, "upc": "75960621681900111", "characters": ["Spider-Man", "Black Cat"],
              "teams": [], "age_rating": "Teen Plus", "series_type": "Single Issue", "story_titles": ["Birth Date"],
              "publisher": "Marvel"},
}


def _parse(xml: bytes) -> dict:
    root = ET.fromstring(xml)
    assert root.tag == "ComicInfo"
    return {c.tag: c.text for c in root}


def test_a_full_record_reads_back_as_clean_comicinfo():
    f = _parse(ci.build(SERIES, ROW, page_count=40))
    assert f["Title"] == "Birth Date" and f["Series"] == "Midnight Spider-Man" and f["Number"] == "1"
    assert (f["Year"], f["Month"], f["Day"]) == ("2026", "12", "1")                 # the cover date, by tradition
    assert f["Writer"] == "Phillip Kennedy Johnson"
    assert f["Penciller"] == "ScieTronc, Someone Else" and f["Inker"] == "ScieTronc, Someone Else"   # 'Artist' = both
    assert f["CoverArtist"] == "Steve Beach, Ryan Stegman"
    assert "Editor" not in f                                                         # the masthead isn't credits
    assert f["GTIN"] == "75960621681900111" and f["AgeRating"] == "Teen" and f["CommunityRating"] == "4.2"
    assert f["PageCount"] == "40" and f["LanguageISO"] == "en" and f["Format"] == "Series"
    assert "Metron issue 181715" in f["Notes"] and "ComicVine issue 1196213" in f["Notes"]
    assert "Teams" not in f                                                          # unknown = absent, never empty


def test_numbers_and_dates_read_like_readers_expect():
    assert ci._number(605.1) == "605.1" and ci._number(12.0) == "12" and ci._number(0.5) == "1/2"
    assert ci._date({"cover_date": "1963-09-00"}) == ("1963", "9", "")
    assert ci._date({"store_date": "2011-07"}) == ("2011", "7", "")
    assert ci._date({}) == ("", "", "")


def test_messy_text_is_sanitised():
    row = dict(ROW, desc="Line one\x0b\n\n  line   two & <three>", extra={"title": "A\x00Title"})
    xml = ci.build(SERIES, row)
    f = _parse(xml)                                                                  # parses = valid XML
    assert f["Summary"] == "Line one line two & <three>" and f["Title"] == "ATitle"


def test_a_thin_record_writes_only_what_it_knows():
    f = _parse(ci.build({"title": "Tank Girl"}, {"number": 3.0, "source": "comicinfo"}))
    assert set(f) == {"Series", "Number", "Notes"}


def test_same_record_same_digest_and_a_change_shows():
    a = ci.build(SERIES, ROW, 40)
    assert ci.digest(a) == ci.digest(ci.build(SERIES, ROW, 40))
    assert ci.digest(a) != ci.digest(ci.build(SERIES, dict(ROW, desc="changed"), 40))
