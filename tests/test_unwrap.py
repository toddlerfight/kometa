"""A .zip whose only entry is another archive is packaging, not a comic.

GetComics serves a real slice of its catalogue double-bagged, and ensure_cbz only
ever looked at the OUTER file's magic bytes — a ZIP wrapping a RAR passed straight
through untouched. Four files landed that way in one session (Secret Wars #5-#8)
and read as unowned forever after: ownership opens an archive and refuses one that
can't produce a page, correctly, so they sat in the library counting for nothing.
"""
import io
import zipfile

from kometa.downloader import ensure_cbz


def _zip_of_images(path, pages=3):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i:03d}.jpg", b"\xff\xd8\xff\xe0 jpeg-ish")
    return path


def _wrap(path, inner_name, inner_bytes):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(inner_name, inner_bytes)
    return path


def _pages(path):
    with zipfile.ZipFile(path) as z:
        return len([n for n in z.namelist() if n.lower().endswith(".jpg")])


class TestUnwrapSingleArchive:
    def test_zip_wrapping_a_zip_is_unwrapped(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(3):
                z.writestr(f"{i:03d}.jpg", b"\xff\xd8\xff\xe0 jpeg-ish")
        outer = tmp_path / "Secret Wars #006.cbz"
        _wrap(outer, "Secret Wars 06 (of 08) (2015) GetComics.INFO.cbz", buf.getvalue())
        assert _pages(outer) == 0          # the bug: zero pages as delivered
        out = ensure_cbz(str(outer))
        assert _pages(out) == 3

    def test_plain_comic_is_untouched(self, tmp_path):
        p = _zip_of_images(tmp_path / "Saga 001.cbz")
        before = p.read_bytes()
        out = ensure_cbz(str(p))
        assert out == str(p)
        assert p.read_bytes() == before

    def test_single_image_entry_is_not_a_wrapper(self, tmp_path):
        p = tmp_path / "odd.cbz"
        _wrap(p, "cover.jpg", b"\xff\xd8\xff\xe0 jpeg-ish")
        out = ensure_cbz(str(p))
        assert out == str(p)

    def test_multi_entry_zip_is_not_a_wrapper(self, tmp_path):
        p = tmp_path / "two.cbz"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("a.cbr", b"Rar!\x1a\x07\x01\x00")
            z.writestr("b.cbr", b"Rar!\x1a\x07\x01\x00")
        assert ensure_cbz(str(p)) == str(p)

    def test_failed_unwrap_leaves_the_original_intact(self, tmp_path):
        # Inner RAR is a stub with no real content — the repack can't produce
        # pages, so the swap must not happen and the delivered file must survive.
        p = tmp_path / "Secret Wars #007.cbz"
        _wrap(p, "Secret Wars 007 (2016).cbr", b"Rar!\x1a\x07\x01\x00 stub")
        before = p.read_bytes()
        out = ensure_cbz(str(p))
        assert out == str(p)
        assert p.exists() and p.read_bytes() == before

    def test_nesting_terminates(self, tmp_path):
        # Pathological russian-doll input must not recurse forever.
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("000.jpg", b"\xff\xd8\xff\xe0 jpeg-ish")
        data = payload.getvalue()
        for i in range(6):
            nxt = io.BytesIO()
            with zipfile.ZipFile(nxt, "w") as z:
                z.writestr(f"layer{i}.cbz", data)
            data = nxt.getvalue()
        p = tmp_path / "doll.cbz"
        p.write_bytes(data)
        ensure_cbz(str(p))   # must return, not hang or blow the stack
