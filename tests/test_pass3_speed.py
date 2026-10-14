"""Audit 2026-10-15, pass 3: the family map is cached, covers answer 304, the
page cache keeps a byte count, On Deck reads no page lists."""
import asyncio
import os
import sys

import pytest
from fastapi import FastAPI, Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_auth import TestClient                                  # the plain-ASGI client

import kometa.db as db
import kometa.main as main
import kometa.reader as rd


def test_family_map_is_computed_once_until_the_series_table_changes(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.forget_families()
    sid = db.add_series(title="Saga", publisher="Image", folder_path="/x/Saga", path=db_path)
    calls = []
    real = db.get_all_series_summaries
    monkeypatch.setattr(db, "get_all_series_summaries", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    main._family_for(sid)
    main._family_for(sid)
    assert len(calls) == 1
    db.add_series(title="Saga: Compendium", publisher="Image", folder_path="/x/Saga Compendium", path=db_path)
    main._family_for(sid)                                              # the table changed: recomputed
    assert len(calls) == 2
    main.forget_families()


def test_a_matching_etag_is_answered_with_304_and_no_body():
    app = FastAPI()

    @app.get("/cover")
    def cover():
        return Response(content=b"jpeg-bytes", media_type="image/jpeg", headers={"ETag": '"abc"', "Cache-Control": "public, max-age=1"})
    app.add_middleware(main._RequestWrap)
    c = TestClient(app)
    full = c.get("/cover")
    assert full.status_code == 200 and full.content == b"jpeg-bytes" and full.headers["etag"] == '"abc"'
    r = c.get("/cover", headers={"if-none-match": '"abc"'})
    assert r.status_code == 304 and r.content == b"" and "content-length" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/cover", headers={"if-none-match": '"other"'}).status_code == 200


def test_stored_covers_carry_an_etag(tmp_path):
    from kometa import images
    p = tmp_path / "c.jpg"; p.write_bytes(b"\xff\xd8\xff" + b"\0" * 10)
    tag = images.file_etag(str(p))
    assert tag.startswith('"') and tag.endswith('"') and "-" in tag
    assert images.file_etag(str(tmp_path / "missing.jpg")) == ""


def test_page_cache_byte_count_drives_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(tmp_path / "pc"))
    monkeypatch.setattr(rd, "PAGE_CACHE_MAX_BYTES", 10_000)
    rd._cache_bytes = 0
    for bid in (1, 2, 3):
        rd._write_cached(str(tmp_path / "pc" / str(bid) / "v" / "1_480.jpg"), b"x" * 4_000)
        os.utime(str(tmp_path / "pc" / str(bid)), (bid, bid))           # 1 is the oldest-read
    assert rd._cache_bytes == 12_000                                      # counted, not walked
    import threading
    for t in threading.enumerate():
        if t.name.startswith("page-cache"):
            t.join(10)
    assert not os.path.exists(str(tmp_path / "pc" / "1"))                 # over the cap: the oldest went
    assert os.path.exists(str(tmp_path / "pc" / "3"))
    assert rd._cache_bytes == 8_000                                       # re-measured after the walk


def test_on_deck_queries_leave_the_page_lists_behind():
    import inspect
    import kometa.ondeck as od
    src = inspect.getsource(od)
    assert "SELECT b.*" not in src and "pages_json" not in od.BOOK_COLS
