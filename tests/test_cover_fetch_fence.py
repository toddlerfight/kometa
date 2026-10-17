"""Catalogue-cover fetches only go to cover hosts and only ever serve an image
(images.cover_url_ok, thumbnails._image_or_none, downloader._download_cover).
Security review 2026-10-11 item 3: a variant pick's URL came from the client, was
fetched from anywhere, and served back with whatever Content-Type the far end claimed."""
import pytest

import kometa.downloader as dl
import kometa.thumbnails as th
from kometa import images

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 100


@pytest.mark.parametrize("url", ["https://static.metron.cloud/media/issue/x.jpg", "https://s3.amazonaws.com/comicgeeks/comics/covers/large-1.jpg",
                                 "https://comicvine.gamespot.com/a/uploads/x.jpg", "https://static.wikia.nocookie.net/marvel/images/x.jpg",
                                 "https://comicbookroundup.com/img/covers/x.jpg"])
def test_cover_hosts_pass(url):
    assert images.cover_url_ok(url)


@pytest.mark.parametrize("url", ["http://[fd00::5]:5000/webapi/", "http://localhost:6969/api/config", "http://prowlarr:9696/api/v1/indexer",
                                 "http://169.254.169.254/latest/meta-data/", "https://s3.amazonaws.com/somebody-else/x.jpg",
                                 "file:///data/kometa.db", "https://attacker.example/x.jpg", "", None])
def test_anything_else_is_refused(url):
    assert not images.cover_url_ok(url)


class _Resp:
    def __init__(self, body, ctype="image/jpeg"):
        self.ok, self.content, self.status_code, self.headers = True, body, 200, {"content-type": ctype}


@pytest.fixture
def no_disk(monkeypatch):
    monkeypatch.setattr(th, "_read_cached", lambda p: None)
    monkeypatch.setattr(th, "_write_cached", lambda p, b: None)


def test_an_internal_address_is_never_asked(no_disk, monkeypatch):
    asked = []
    monkeypatch.setattr(th._img_session, "get", lambda url, **k: asked.append(url) or _Resp(JPEG))
    assert th._image_or_none("http://[fd00::5]:8080/admin") is None and asked == []


def test_only_an_image_comes_back_typed_by_its_own_bytes(no_disk, monkeypatch):
    monkeypatch.setattr(th._img_session, "get", lambda url, **k: _Resp(b"<html><script>alert(1)</script></html>", "text/html"))
    assert th._image_or_none("https://static.metron.cloud/x.jpg") is None
    monkeypatch.setattr(th._img_session, "get", lambda url, **k: _Resp(JPEG, "text/html"))
    r = th._image_or_none("https://static.metron.cloud/x.jpg")
    assert r is not None and r.media_type == "image/jpeg"


def test_a_cached_non_image_isnt_served(monkeypatch):
    monkeypatch.setattr(th, "_read_cached", lambda p: b"<html>old</html>")
    monkeypatch.setattr(th, "_write_cached", lambda p, b: None)
    monkeypatch.setattr(th._img_session, "get", lambda url, **k: _Resp(b"<html>still not</html>", "text/html"))
    assert th._image_or_none("https://static.metron.cloud/x.jpg") is None


def test_a_variant_written_into_a_comic_is_a_cover_hosts_image(monkeypatch):
    asked = []
    monkeypatch.setattr(dl.requests, "get", lambda url, timeout: asked.append(url) or _Resp(b"<html>"))
    assert dl._download_cover({"id": "x", "large": "http://[fd00::5]/secret"}) is None and asked == []
    assert dl._download_cover({"id": "x", "large": "https://static.metron.cloud/v/x.jpg"}) is None     # not an image
    monkeypatch.setattr(dl.requests, "get", lambda url, timeout: _Resp(JPEG))
    assert dl._download_cover({"id": "x", "large": "https://static.metron.cloud/v/x.jpg"}) == JPEG
