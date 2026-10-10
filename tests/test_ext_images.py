"""Catalogue-card covers through Kometa (kometa/images.py serve_external): a day on disk, allow-listed."""
import os

import kometa.images as im


def test_allow_list_and_key():
    assert im.ext_allowed("https://static.metron.cloud/media/issue/x.jpg")
    assert im.ext_allowed("https://s3.amazonaws.com/comicgeeks/comics/covers/medium-1.jpg")
    assert not im.ext_allowed("https://evil.example/x.jpg") and not im.ext_allowed("file:///etc/passwd")
    assert im.ext_key("https://a/b.jpg").startswith("ext/") and im.ext_key("https://a/b.jpg") != im.ext_key("https://a/c.jpg")


def test_external_cover_is_fetched_once_then_served_from_disk_then_pruned(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    calls = []
    def http(url):
        calls.append(url); return b"\xff\xd8\xff\xe0 jpeg-ish"
    url = "https://static.metron.cloud/media/issue/x.jpg"
    r1 = im.serve_external(url, db_path, http=http); r2 = im.serve_external(url, db_path, http=http)
    assert r1.headers["X-Kometa-Image"] == "fetched" and r2.headers["X-Kometa-Image"] == "disk" and len(calls) == 1
    row = im.get(im.ext_key(url), db_path); assert os.path.exists(row["path"])
    import kometa.db as db
    with db._connect(db_path) as c:
        c.execute("UPDATE image_record SET fetched_at = datetime('now', '-25 hours') WHERE key LIKE 'ext/%'")
    assert im.prune_external(db_path) == 1 and not os.path.exists(row["path"]) and im.get(im.ext_key(url), db_path) is None
    assert im.serve_external(url, db_path, http=http).headers["X-Kometa-Image"] == "fetched" and len(calls) == 2
