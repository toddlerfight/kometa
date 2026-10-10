"""Covers from the files we hold (kometa/covers.py): page 1 into the store,
a cut-short page 1 skipped, the trickle's queue, and the cover chain's order."""
import io
import zipfile

import pytest
from PIL import Image

import kometa.db as db
import kometa.covers as cv
import kometa.images as im


def _jpeg(w=600, h=900, color=(120, 30, 30)) -> bytes:
    buf = io.BytesIO(); Image.new("RGB", (w, h), color).save(buf, "JPEG"); return buf.getvalue()


def _cbz(path, pages, comicinfo: str | None = None):
    with zipfile.ZipFile(path, "w") as zf:
        for i, data in enumerate(pages):
            zf.writestr(f"p{i:03d}.jpg", data)
        if comicinfo:
            zf.writestr("ComicInfo.xml", comicinfo)
    return str(path)


@pytest.fixture
def store(db_path, tmp_path, monkeypatch):
    import kometa.reader as rd
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(cv, "DB_PATH", db_path)
    monkeypatch.setattr(rd, "DB_PATH", db_path)
    monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    return db_path


def _owned(db_path, tmp_path, title="Saga", numbers=(1, 2), truncated=()):
    folder = tmp_path / title; folder.mkdir(exist_ok=True)
    sid = db.add_series(title=title, publisher="Image", folder_path=str(folder), on_pull_list=False, path=db_path)
    shelf = db.upsert_shelf_series(str(folder), title, "Image", sid, len(numbers), "2026-10-10T00:00:00Z", db_path)
    rows = []
    for n in numbers:
        page = _jpeg()
        if n in truncated:
            page = page[: len(page) // 2]
        f = _cbz(folder / f"{title} #{n:03d}.cbz", [page, _jpeg()])
        db.upsert_issue_status(sid, float(n), "2012-01-01", 1, path=db_path)
        rows.append((f, 10, 1.0, float(n), shelf, sid))
    db.index_books(rows, db_path)
    return sid, [db.get_book_by_path(r[0], db_path) for r in rows]


def test_page_one_goes_into_the_store_under_its_issue_and_series_keys(store, tmp_path):
    sid, books = _owned(store, tmp_path, numbers=(3, 1))
    assert cv.generate_for_book(books[0], store) == cv.file_key(sid, 3)        # #3 first: the series cover for now
    assert im.get(cv.series_file_key(sid), store)["url"] == "issue:3"
    assert cv.generate_for_book(books[1], store) == cv.file_key(sid, 1)
    assert im.get(cv.series_file_key(sid), store)["url"] == "issue:1"          # the lowest owned issue fronts the run
    assert im.local_path(cv.file_key(sid, 1), store).endswith("issue/%d/1/file.jpg" % sid)


def test_a_cut_short_page_one_is_skipped_and_remembered(store, tmp_path):
    sid, books = _owned(store, tmp_path, numbers=(1,), truncated=(1,))
    assert cv.generate_for_book(books[0], store) is None
    row = im.get(cv.file_key(sid, 1), store)
    assert row["path"] is None and row["failed_at"] and row["url"] == "truncated"
    assert cv.pending_books(store) == []                                       # not retried this week


def test_trickle_takes_what_the_store_lacks_pull_list_first(store, tmp_path):
    a, _ = _owned(store, tmp_path, title="Alpha", numbers=(1,))
    b, _ = _owned(store, tmp_path, title="Pulled", numbers=(1,))
    db.set_pull_list(b, True, store)
    assert [x["tracked_series_id"] for x in cv.pending_books(store)] == [b, a]
    out = cv.covers_trickle(path=store)
    assert out["covers"] == 2 and cv.pending_books(store) == []
    assert cv.backlog(store)["file_covers"] == 2


def test_generate_for_path_keys_by_the_file_name(store, tmp_path):
    sid, books = _owned(store, tmp_path, numbers=(7,))
    assert cv.generate_for_path(books[0]["path"], sid, store) == cv.file_key(sid, 7)
    assert cv.generate_for_path(str(tmp_path / "Saga" / "nothing.cbz"), sid, store) is None


# --- the issue route's order: variant → file → catalogue → … → Komga only when asked ------
@pytest.fixture
def chain(store, monkeypatch):
    import kometa.thumbnails as th
    monkeypatch.setattr(th, "DB_PATH", store)
    asked = {"komga": 0, "catalogue": []}

    class _K:
        base_url = "http://komga"

    monkeypatch.setattr(th, "_komga", lambda: (asked.__setitem__("komga", asked["komga"] + 1) or _K()))
    monkeypatch.setattr(th, "_komga_thumb", lambda k, url, key: th._jpeg(_jpeg(100, 150)))
    monkeypatch.setattr(th, "_image_or_none", lambda url, *a, **k: asked["catalogue"].append(url) or th._jpeg(_jpeg(300, 450)))
    monkeypatch.setattr(im, "serve", lambda *a, **k: None)
    return th, asked


def _with_catalogue(db_path, sid, n, komga_book="B1"):
    with db._connect(db_path) as conn:
        conn.execute("UPDATE issue_status SET metron_image = 'https://cat/cover.jpg', komga_book_id = ? "
                     "WHERE tracked_series_id = ? AND number = ?", (komga_book, sid, n))


def test_the_file_cover_beats_the_catalogue_for_an_owned_issue(chain, store, tmp_path):
    th, asked = chain
    sid, _ = _owned(store, tmp_path, numbers=(1,))
    _with_catalogue(store, sid, 1)
    r = th.issue_thumbnail(sid, 1.0)
    assert r.headers["X-Kometa-Source"] == "file" and asked["catalogue"] == [] and asked["komga"] == 0
    assert im.local_path(cv.file_key(sid, 1), store)                           # kept for next time


def test_a_cut_short_file_cover_falls_through_to_the_catalogue(chain, store, tmp_path):
    th, asked = chain
    sid, _ = _owned(store, tmp_path, numbers=(1,), truncated=(1,))
    _with_catalogue(store, sid, 1)
    r = th.issue_thumbnail(sid, 1.0)
    assert r.headers["X-Kometa-Source"] == "catalogue" and asked["catalogue"] == ["https://cat/cover.jpg"]
    assert im.get(cv.file_key(sid, 1), store)["failed_at"]                     # remembered, not retried per render
    assert asked["komga"] == 0


def test_komga_is_not_asked_unless_the_flag_is_on(chain, store, tmp_path, monkeypatch):
    th, asked = chain
    sid, _ = _owned(store, tmp_path, numbers=(1,), truncated=(1,))
    _with_catalogue(store, sid, 1)
    with db._connect(store) as conn:                                            # no catalogue art either
        conn.execute("UPDATE issue_status SET metron_image = NULL WHERE tracked_series_id = ?", (sid,))
    assert th.issue_thumbnail(sid, 1.0).status_code == 404 and asked["komga"] == 0
    th._thumb_misses.clear()
    db.set_config({"komga_covers": "1"}, store)
    r = th.issue_thumbnail(sid, 1.0)
    assert r.status_code == 200 and r.headers["X-Kometa-Source"] == "komga" and asked["komga"] == 1
