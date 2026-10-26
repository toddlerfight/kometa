"""Want to read as a reading list (kometa/wantread.as_list): the On Deck row's pile, pinned
first on Lists and in the OPDS feed, under a reserved id — nothing to keep in sync."""
import pytest
from fastapi import HTTPException

import kometa.readlists as rl
import kometa.wantread as wr
from kometa import opds, related
from tests.test_ondeck import lib  # noqa: F401  (Saga #1–3 + a TPB, Low #1–2; Saga #4 and Low #3 not owned)


@pytest.fixture
def want(lib, monkeypatch):  # noqa: F811
    for m in (wr, rl, opds):
        monkeypatch.setattr(m, "DB_PATH", lib["db"])
    monkeypatch.setattr(related, "_resolved_lists", lambda path: [])
    return lib


def test_it_is_the_rows_pile_in_a_lists_shape(want):
    wr.add(series_id=want["saga"], number=4, path=want["db"])                  # not here yet
    wr.add(book_id=want["b"][("low", 1)], path=want["db"])
    res = wr.as_list(want["db"])
    assert res["id"] == wr.WANT_LIST_ID and res["name"] == "Want to read" and res["pinned"]
    assert [(e["series"], e["number"], e["status"]) for e in res["entries"]] == [("Saga", "4", "missing"), ("Low", "1", "owned")]
    assert res["entries"][0]["cover"].endswith("/issues/4/thumbnail") and res["entries"][1]["books"][0]["id"] == want["b"][("low", 1)]
    assert res["continue"] == want["b"][("low", 1)]


def test_it_leads_the_lists_only_while_it_holds_anything(want):
    assert [l["name"] for l in rl.get_lists(want["db"])] == []
    wr.add(book_id=want["b"][("low", 1)], path=want["db"])
    lists = rl.get_lists(want["db"])
    assert lists[0]["name"] == "Want to read" and lists[0]["cover_book_id"] == want["b"][("low", 1)]
    assert not lists[0]["can_share"] and lists[0]["mine"]


def test_it_opens_reads_on_and_cannot_be_removed_but_its_entries_can(want):
    a = wr.add(book_id=want["b"][("low", 1)], path=want["db"])
    wr.add(book_id=want["b"][("low", 2)], path=want["db"])
    assert rl.api_list(wr.WANT_LIST_ID)["total"] == 2
    assert rl.api_next(wr.WANT_LIST_ID, want["b"][("low", 1)]) == {"book_id": want["b"][("low", 2)]}
    assert rl.api_next(wr.WANT_LIST_ID, 999999) == {"book_id": want["b"][("low", 1)]}      # just finished: it left, next is the head
    with pytest.raises(HTTPException) as e:
        rl.api_delete(wr.WANT_LIST_ID)
    assert e.value.status_code == 403
    rl.api_delete_item(wr.WANT_LIST_ID, a["id"])
    assert [e["series"] + e["number"] for e in rl.api_list(wr.WANT_LIST_ID)["entries"]] == ["Low2"]
    with pytest.raises(HTTPException):
        rl.api_delete_item(wr.WANT_LIST_ID, a["id"])                                  # gone already


def test_opds_lists_it_first_with_the_books_you_have(want):
    wr.add(book_id=want["b"][("low", 1)], path=want["db"])
    assert "Want to read" in opds.lists_feed(want["db"]).split("<entry>")[1]
    assert f"/opds/books/{want['b'][('low', 1)]}" in opds.one_list_feed(wr.WANT_LIST_ID, want["db"])
