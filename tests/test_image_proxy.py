"""/api/img and the image store's fetch: an allow-listed, capped, images-only proxy."""
import pytest

from kometa import images
import kometa.thumbnails as thumbs
from kometa.main import _redact


def test_s3_is_allowed_only_under_the_locg_bucket():
    assert images.ext_allowed("https://s3.amazonaws.com/comicgeeks/comics/covers/large-1.jpg")
    assert not images.ext_allowed("https://s3.amazonaws.com/somebody-else/huge.bin")
    assert images.ext_allowed("https://static.metron.cloud/media/issue/2024/x.jpg")
    assert not images.ext_allowed("https://evil.example/x.jpg")
    assert not images.ext_allowed("ftp://static.metron.cloud/x.jpg")


class _Resp:
    def __init__(self, body=b"", headers=None, ok=True):
        self._body, self.headers, self.ok = body, headers or {}, ok
    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _Session:
    def __init__(self, resp): self.resp = resp
    def get(self, url, **k): return self.resp


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 100


def test_download_keeps_a_real_image(monkeypatch):
    monkeypatch.setattr(thumbs, "_img_session", _Session(_Resp(JPEG)))
    assert images._download("https://static.metron.cloud/a.jpg") == JPEG


def test_download_refuses_a_declared_oversize_body(monkeypatch):
    monkeypatch.setattr(thumbs, "_img_session", _Session(_Resp(JPEG, {"content-length": str(images.MAX_IMAGE_BYTES + 1)})))
    assert images._download("https://static.metron.cloud/a.jpg") is None


def test_download_stops_at_the_cap_when_the_body_lies(monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 1000)
    monkeypatch.setattr(thumbs, "_img_session", _Session(_Resp(JPEG + b"\0" * 2000)))
    assert images._download("https://static.metron.cloud/a.jpg") is None


def test_download_refuses_what_is_not_an_image(monkeypatch):
    monkeypatch.setattr(thumbs, "_img_session", _Session(_Resp(b"<html>not a cover</html>")))
    assert images._download("https://static.metron.cloud/a.jpg") is None


def test_each_account_has_an_hourly_budget_of_new_images(monkeypatch):
    monkeypatch.setattr(images, "EXT_FETCH_BUDGET", 3)
    images._ext_budget.clear()
    assert [images.ext_budget_ok("u-tess") for _ in range(4)] == [True, True, True, False]
    assert images.ext_budget_ok("u-other")


def test_errors_from_test_endpoints_do_not_repeat_the_key():
    assert _redact("GET http://sab:8080/api?apikey=abcd1234 failed", "abcd1234") == "GET http://sab:8080/api?apikey=*** failed"
    assert _redact("boom", None) == "boom"
