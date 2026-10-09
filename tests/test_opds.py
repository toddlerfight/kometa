"""OPDS feeds (kometa/opds.py): the shelf as Atom, with PSE stream links."""
import re
import xml.etree.ElementTree as ET

import kometa.db as db
import kometa.opds as op
from tests.conftest import make_cbz

NS = {"a": "http://www.w3.org/2005/Atom", "pse": "http://vaemendis.net/opds-pse/ns"}


def _shelf(db_path, tmp_path):
    folder = tmp_path / "comics" / "Image Comics" / "Saga"
    folder.mkdir(parents=True)
    files = ["Saga #001.cbz", "Saga #002.cbz", "Saga - Compendium (2019).cbz"]
    for f in files:
        make_cbz(folder / f)
    sid = db.upsert_shelf_series(str(folder), "Saga", "Image Comics", None, 3, "2026-10-09T00:00:00Z", db_path)
    db.index_books([(str(folder / files[0]), 10, 1.0, 1.0, sid, None), (str(folder / files[1]), 10, 1.0, 2.0, sid, None),
                    (str(folder / files[2]), 10, 1.0, None, sid, None)], db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE books SET page_count = 22")
    return sid, [db.get_book_by_path(str(folder / f), db_path)["id"] for f in files]


def test_root_and_series_feeds_parse_and_link(db_path, tmp_path):
    sid, _ = _shelf(db_path, tmp_path)
    root = ET.fromstring(op.root_feed())
    assert [e.find("a:title", NS).text for e in root.findall("a:entry", NS)] == ["Keep reading", "Recently added", "All series"]
    ser = ET.fromstring(op.series_feed(0, db_path))
    e = ser.find("a:entry", NS)
    assert e.find("a:title", NS).text == "Saga"
    assert e.find("a:link[@rel='subsection']", NS).get("href") == f"/opds/series/{sid}"
    assert ser.find("a:link[@rel='next']", NS) is None


def test_books_carry_download_and_pse_stream_with_last_read(db_path, tmp_path):
    sid, ids = _shelf(db_path, tmp_path)
    db.set_progress("me", ids[1], 7, False, "2026-10-09T09:30:00Z", db_path)
    feed = ET.fromstring(op.one_series_feed(sid, db_path))
    entries = feed.findall("a:entry", NS)
    assert [e.find("a:title", NS).text for e in entries] == ["Saga #1", "Saga #2", "Saga - Compendium (2019)"]
    acq = entries[0].find("a:link[@rel='http://opds-spec.org/acquisition']", NS)
    assert acq.get("href") == f"/opds/books/{ids[0]}/file" and acq.get("type") == "application/vnd.comicbook+zip"
    pse = entries[1].find("a:link[@rel='http://vaemendis.net/opds-pse/stream']", NS)
    assert pse.get("href") == f"/opds/books/{ids[1]}/pages/{{pageNumber}}?maxWidth={{maxWidth}}"
    assert pse.get("{http://vaemendis.net/opds-pse/ns}count") == "22"
    assert pse.get("{http://vaemendis.net/opds-pse/ns}lastRead") == "7"
    assert entries[0].find("a:link[@rel='http://vaemendis.net/opds-pse/stream']", NS).get("{http://vaemendis.net/opds-pse/ns}lastRead") is None


def test_keep_reading_recent_and_search(db_path, tmp_path):
    sid, ids = _shelf(db_path, tmp_path)
    db.set_progress("me", ids[0], 3, False, "2026-10-09T09:30:00Z", db_path)
    db.set_progress("me", ids[1], 22, True, "2026-10-08T09:30:00Z", db_path)
    kr = ET.fromstring(op.keep_reading_feed(db_path))
    assert [e.find("a:title", NS).text for e in kr.findall("a:entry", NS)] == ["Saga #1"]      # finished #2 isn't 'keep reading'
    assert len(ET.fromstring(op.recent_feed(db_path)).findall("a:entry", NS)) == 3
    sr = ET.fromstring(op.search_feed("sag", db_path))
    assert sr.find("a:entry/a:title", NS).text == "Saga"
    assert ET.fromstring(op.search_feed("", db_path)).find("a:entry", NS) is None


def test_titles_with_ampersands_are_well_formed(db_path, tmp_path):
    folder = tmp_path / "comics" / "DC" / "Batman & Robin"
    folder.mkdir(parents=True)
    make_cbz(folder / "Batman & Robin #001.cbz")
    sid = db.upsert_shelf_series(str(folder), "Batman & Robin", "DC", None, 1, "2026-10-09T00:00:00Z", db_path)
    db.index_books([(str(folder / "Batman & Robin #001.cbz"), 10, 1.0, 1.0, sid, None)], db_path)
    ET.fromstring(op.series_feed(0, db_path)); ET.fromstring(op.one_series_feed(sid, db_path))
