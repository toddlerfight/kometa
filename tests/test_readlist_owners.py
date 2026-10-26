"""Reading lists are per reader (2026-10-26): testers were seeing the owner's lists.
Each list has an owner; a reader sees their own and the owner's shared ones, and
anything else is no list at all — the API, OPDS and the Because rows alike."""
import sqlite3

import pytest
from fastapi import HTTPException

import kometa.auth as auth
import kometa.db as db
import kometa.readlists as rl
from kometa import listget, opds, related

CBL = lambda name: f"""<?xml version="1.0"?><ReadingList><Name>{name}</Name><Books>
<Book Series="Hellboy: Seed of Destruction" Number="1" Volume="1994" />
</Books></ReadingList>""".encode()

TESTER = {"id": "u-tester", "username": "tester", "role": "reader"}
OTHER = {"id": "u-other", "username": "other", "role": "reader"}
ADMIN = {"id": auth.OWNER, "username": "owner", "role": "admin"}


@pytest.fixture
def lists(db_path, monkeypatch):
    monkeypatch.setattr(rl, "DB_PATH", db_path)
    monkeypatch.setattr(opds, "DB_PATH", db_path)
    monkeypatch.setattr(related, "_resolved_lists", lambda path: [])   # no warm copy: every read resolves live
    return db_path


def as_(user):
    class _As:
        def __enter__(self):
            self.tok = auth.as_reader(user)
        def __exit__(self, *a):
            auth._CURRENT.reset(self.tok)
    return _As()


def names(path):
    return [l["name"] for l in rl.get_lists(path)]


def test_a_reader_sees_their_own_lists_and_the_owners_shared_ones(lists):
    with as_(ADMIN):
        mine = rl.import_cbl(CBL("Owner's private"), path=lists)
        shared = rl.import_cbl(CBL("Owner's shared"), path=lists)
        rl.set_shared(shared, True, path=lists)
    with as_(TESTER):
        rl.import_cbl(CBL("Tester's own"), path=lists)
        assert names(lists) == ["Owner's shared", "Tester's own"]
        assert {l["name"]: l["mine"] for l in rl.get_lists(lists)} == {"Owner's shared": False, "Tester's own": True}
    with as_(OTHER):
        assert names(lists) == ["Owner's shared"]
    with as_(ADMIN):
        assert names(lists) == ["Owner's private", "Owner's shared"]          # a reader's list is theirs, not his
        assert rl.visible(mine, path=lists)


def test_someone_elses_list_is_no_list_at_all(lists):
    with as_(ADMIN):
        private = rl.import_cbl(CBL("Private"), path=lists)
    with as_(TESTER):
        for call in (lambda: rl.api_list(private), lambda: rl.api_next(private, 1),
                     lambda: rl.api_fill_covers(private), lambda: rl.api_delete(private)):
            with pytest.raises(HTTPException) as e:
                call()
            assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            opds.one_list_feed(private, lists)
        assert e.value.status_code == 404


def test_a_shared_list_reads_but_only_its_owner_changes_it(lists):
    with as_(ADMIN):
        lid = rl.import_cbl(CBL("Shared"), path=lists)
        rl.api_share(lid, rl.ShareRequest(shared=True))
    with as_(TESTER):
        assert rl.api_list(lid)["mine"] is False
        with pytest.raises(HTTPException) as e:
            rl.api_delete(lid)
        assert e.value.status_code == 403
    with as_(ADMIN):
        assert rl.api_list(lid)["mine"] is True
        assert "owner" not in rl.api_list(lid)                 # who owns it stays on the server


def test_the_same_name_from_two_readers_is_two_lists(lists):
    with as_(ADMIN):
        a = rl.import_cbl(CBL("Secret Wars"), path=lists)
    with as_(TESTER):
        b = rl.import_cbl(CBL("Secret Wars"), path=lists)
        assert rl.import_cbl(CBL("Secret Wars"), path=lists) == b   # their own re-import: replaced in place
    assert a != b


def test_a_reader_deletes_their_own_list(lists):
    with as_(TESTER):
        lid = rl.import_cbl(CBL("Mine"), path=lists)
        rl.api_delete(lid)
        assert names(lists) == []


def test_lists_from_before_owners_are_the_owners(db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE reading_lists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                  "source TEXT NOT NULL DEFAULT 'cbl', source_ref TEXT, created_at TEXT)")
        c.execute("INSERT INTO reading_lists (name) VALUES ('Old list')")
    rl.ensure_tables(db_path)
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT owner, shared FROM reading_lists").fetchone() == (auth.OWNER, 0)


def test_only_the_owners_lists_can_be_got(lists):
    with as_(TESTER):
        theirs = rl.import_cbl(CBL("Tester's"), path=lists)
    with as_(ADMIN):
        with pytest.raises(HTTPException):
            listget.api_get_missing(theirs)
        with pytest.raises(HTTPException):
            listget.api_get_entry(theirs, 1)


def test_readers_may_add_and_remove_lists_but_not_share_or_get():
    may = auth.reader_may
    assert may("POST", "/api/readlists/import") and may("POST", "/api/readlists/import-url")
    assert may("POST", "/api/listcatalog/import") and may("GET", "/api/listcatalog")
    assert may("DELETE", "/api/readlists/7") and may("DELETE", "/api/readlists/7/items/3")
    assert may("POST", "/api/readlists/7/covers")
    assert not may("PATCH", "/api/readlists/7")
    assert not may("POST", "/api/readlists/7/get-missing") and not may("POST", "/api/readlists/7/items/3/get")


def test_next_on_a_list_only_names_lists_the_reader_can_open(monkeypatch):
    entry = {"position": 2, "series_id": None, "series": "Gap", "status": "missing", "item_id": 9, "cover": "/c"}
    seed = {"position": 1, "series_id": 5, "series": "Seed", "status": "owned", "item_id": 8}
    resolved = [{"id": 1, "name": "Owner's private", "owner": auth.OWNER, "shared": False, "entries": [seed, entry]},
                {"id": 2, "name": "Owner's shared", "owner": auth.OWNER, "shared": True, "entries": [seed, dict(entry, series="Gap 2")]}]
    monkeypatch.setattr(related, "_resolved_lists", lambda path: resolved)
    with as_(TESTER):
        assert [g["list_name"] for g in related._list_gaps_near([5], None, {5: "Seed"})] == ["Owner's shared"]
    with as_(ADMIN):
        assert [g["list_name"] for g in related._list_gaps_near([5], None, {5: "Seed"})] == ["Owner's private", "Owner's shared"]


def test_a_readers_lists_dont_shape_everyones_related_series():
    a, b = {"position": 1, "series_id": 1}, {"position": 2, "series_id": 2}
    resolved = [{"id": 1, "owner": "u-tester", "shared": False, "entries": [a, b]}]
    assert not related._compute_list_neighbours(None, resolved)
    resolved[0]["owner"] = auth.OWNER
    assert related._compute_list_neighbours(None, resolved)[1][2] > 0
