"""The community's lists (kometa/listcatalog.py): DieselTech's repo, searched by name,
added as the asking reader's own."""
import pytest

import kometa.auth as auth
import kometa.readlists as rl
from kometa import listcatalog as cat

PATHS = [
    "Dark Horse/Characters/Hellboy/[Dark Horse] Hellboy (Mignolaverse) TPB Reading Order.cbl",
    "Dark Horse/Characters/Aliens/[Dark Horse] Aliens Publication Order.cbl",
    "Marvel/Events/[Marvel] Secret Wars (2015).cbl",
    "Marvel/Characters/Iron Man/[Marvel] Infamous Iron Man.cbl",
]
CBL = b"""<?xml version="1.0"?><ReadingList><Name>Infamous Iron Man</Name><Books>
<Book Series="Infamous Iron Man" Number="1" Volume="2016" /></Books></ReadingList>"""


@pytest.fixture(autouse=True)
def indexed(monkeypatch, db_path):
    monkeypatch.setattr(rl, "DB_PATH", db_path)
    monkeypatch.setattr(cat, "_index", {"at": 1e12, "paths": list(PATHS)})


def test_every_word_must_match_and_a_name_hit_ranks_first():
    hits = [h["name"] for h in cat.search("dark horse hellboy", PATHS)]
    assert hits == ["[Dark Horse] Hellboy (Mignolaverse) TPB Reading Order"]
    assert [h["name"] for h in cat.search("iron man", PATHS)] == ["[Marvel] Infamous Iron Man"]
    assert cat.search("", PATHS) == []
    assert cat.search("hellboy", PATHS)[0]["folder"] == "Dark Horse › Characters › Hellboy"


def test_adding_imports_the_raw_file_as_the_readers_own():
    asked = []
    def fetch(url, limit):
        asked.append(url)
        return CBL
    tok = auth.as_reader({"id": "u-tester", "username": "tester", "role": "reader"})
    try:
        lid = cat.add(PATHS[3], fetch=fetch)
        assert rl.owns(lid)
        assert [l["name"] for l in rl.get_lists()] == ["Infamous Iron Man"]
    finally:
        auth._CURRENT.reset(tok)
    assert asked == [cat.RAW + "Marvel/Characters/Iron%20Man/%5BMarvel%5D%20Infamous%20Iron%20Man.cbl"]


def test_only_a_path_the_index_holds_is_fetched():
    with pytest.raises(KeyError):
        cat.add("../../etc/passwd.cbl", fetch=lambda *a: pytest.fail("fetched"))


def test_a_github_outage_keeps_the_last_good_index(monkeypatch):
    monkeypatch.setattr(cat, "_index", {"at": 0.0, "paths": list(PATHS)})
    def down(session=None):
        raise ConnectionError("github down")
    monkeypatch.setattr(cat, "_fetch_tree", down)
    assert cat.index() == PATHS
