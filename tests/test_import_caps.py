"""Size caps and the URL importer's fence (readlists.fetch_public, MAX_CBL_BYTES; the
archive readers' entry caps). Security review 2026-10-11 item 8: the importer fetched
any address, bodies had no size limit, zip entries were read whole into memory."""
import socket
import zipfile

import pytest

import kometa.downloader as dl
import kometa.readlists as rl


def _resolver(table):
    def resolve(host, port, type=None):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]
    return resolve


class _Resp:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code, self._body = status, body
        self.headers = {"location": location} if location else {}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)
    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class _Session:
    def __init__(self, routes):
        self.routes, self.asked = routes, []
    def get(self, url, **kw):
        self.asked.append(url)
        return self.routes[url]


DNS = _resolver({"raw.githubusercontent.com": ["185.199.108.133"], "intranet.example": ["fd00::5"],
                 "sneaky.example": ["185.199.108.133", "127.0.0.1"]})


def test_a_public_https_list_is_fetched():
    s = _Session({"https://raw.githubusercontent.com/a/b/x.cbl": _Resp(body=b"<ReadingList/>")})
    assert rl.fetch_public("https://raw.githubusercontent.com/a/b/x.cbl", 1000, s, DNS) == b"<ReadingList/>"


@pytest.mark.parametrize("url", ["http://raw.githubusercontent.com/x.cbl", "https://intranet.example/x.cbl", "https://sneaky.example/x.cbl",
                                 "https://[fd00::5]/x.cbl", "https://nowhere.example/x.cbl", "file:///data/kometa.db"])
def test_anything_not_public_https_is_never_asked(url):
    s = _Session({})
    with pytest.raises(ValueError):
        rl.fetch_public(url, 1000, s, DNS)
    assert s.asked == []


def test_a_redirect_into_the_lan_is_refused():
    s = _Session({"https://raw.githubusercontent.com/x.cbl": _Resp(302, location="https://intranet.example/admin")})
    with pytest.raises(ValueError):
        rl.fetch_public("https://raw.githubusercontent.com/x.cbl", 1000, s, DNS)
    assert s.asked == ["https://raw.githubusercontent.com/x.cbl"]


def test_a_body_past_the_cap_is_dropped():
    s = _Session({"https://raw.githubusercontent.com/x.cbl": _Resp(body=b"x" * 5000)})
    with pytest.raises(ValueError):
        rl.fetch_public("https://raw.githubusercontent.com/x.cbl", 1000, s, DNS)


def test_a_page_entry_that_declares_gigabytes_stops_the_read(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "MAX_PAGE_ENTRY_BYTES", 1000)
    p = tmp_path / "bomb.cbz"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.jpg", b"\xff\xd8\xff" + b"\0" * 10)
        z.writestr("002.jpg", b"\0" * 5000)
    pages = dl._read_archive_pages(str(p))
    assert next(pages)[0] == "001.jpg"
    with pytest.raises(RuntimeError):
        next(pages)
