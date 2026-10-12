"""A bigger stored copy of the same cover stands in for the small one on screen
(kometa/coverupgrade.py). Offline: hand-drawn JPEGs, a throwaway store."""
import pytest

import kometa.db as db
from kometa import coverupgrade, images, thumbnails
from tests.test_marveldc import _jpeg


@pytest.fixture
def store(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(images, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(thumbnails, "DB_PATH", db_path)
    images.ensure_tables(db_path)
    return db_path


def _put(db_path, key, data, source):
    images.store_bytes(key, data, f"https://x/{key}", source, db_path)


def test_bigger_same_cover_stands_in_smaller_or_different_ones_dont(store):
    small, big, other = _jpeg(1, (300, 450)), _jpeg(1, (1200, 1800)), _jpeg(5, (300, 450))
    _put(store, "issue/7/1/main", small, "metron")
    _put(store, "issue/7/1/variant/b", other, "locg")
    _put(store, "issue/7/1/marveldc", big, "marveldc")
    assert coverupgrade.link(7, 1.0, "issue/7/1/marveldc", big, store) == ["issue/7/1/main"]
    assert coverupgrade.better_path("issue/7/1/main", store).endswith("marveldc.jpg")
    assert coverupgrade.better_path("issue/7/1/variant/b", store) is None
    # a smaller stand-in never replaces a bigger one already linked
    _put(store, "issue/7/1/marveldc/v2", _jpeg(1, (600, 900)), "marveldc")
    assert coverupgrade.link(7, 1.0, "issue/7/1/marveldc/v2", _jpeg(1, (600, 900)), store) == []
    assert coverupgrade.better_path("issue/7/1/main", store).endswith("marveldc.jpg")


def test_store_response_and_serve_hand_back_the_bigger_copy(store):
    small, big = _jpeg(2, (300, 450)), _jpeg(2, (1200, 1800))
    _put(store, "issue/8/1/file", small, "file")
    _put(store, "issue/8/1/main", small, "metron")
    _put(store, "issue/8/1/marveldc", big, "marveldc")
    coverupgrade.link(8, 1.0, "issue/8/1/marveldc", big, store)
    assert thumbnails._store_response("issue/8/1/file", "file").body == big
    resp = images.serve("issue/8/1/main", "https://x/issue/8/1/main", "metron", store)
    assert resp.body == big and resp.headers["X-Kometa-Image"] == "upgrade"


def test_backfill_links_old_stand_ins_once(store, monkeypatch):
    small, big = _jpeg(3, (300, 450)), _jpeg(3, (1200, 1800))
    _put(store, "issue/9/1/main", small, "metron")
    _put(store, "issue/9/1/marveldc", big, "marveldc")
    _put(store, "issue/9/1/marveldc/new", _jpeg(6, (1200, 1800)), "marveldc")   # a cover we didn't have: links nothing
    assert coverupgrade.backfill(store) == 1
    hashed = []
    monkeypatch.setattr(coverupgrade, "link", lambda *a, **k: hashed.append(a) or [])
    assert coverupgrade.backfill(store) == 0 and hashed == []            # nothing looked at twice


def test_no_table_yet_means_no_upgrade(db_path):
    assert coverupgrade.better_path("issue/1/1/main", db_path) is None


def test_a_textless_or_redressed_printing_is_not_the_same_image(store):
    from PIL import Image, ImageDraw
    import io

    def cover(logo: bool, size):
        im = Image.open(io.BytesIO(_jpeg(4, (600, 900)))).convert("RGB")
        if logo:
            ImageDraw.Draw(im).rectangle([40, 20, 560, 150], fill=(200, 20, 20))   # the trade dress
        im = im.resize(size)
        b = io.BytesIO(); im.save(b, "JPEG", quality=90); return b.getvalue()
    logo_small, textless_big, logo_big = cover(True, (300, 450)), cover(False, (1200, 1800)), cover(True, (1200, 1800))
    _put(store, "issue/10/1/main", logo_small, "metron")
    _put(store, "issue/10/1/marveldc/virgin", textless_big, "marveldc")
    assert coverupgrade.link(10, 1.0, "issue/10/1/marveldc/virgin", textless_big, store) == []
    _put(store, "issue/10/1/marveldc", logo_big, "marveldc")
    assert coverupgrade.link(10, 1.0, "issue/10/1/marveldc", logo_big, store) == ["issue/10/1/main"]


def test_marvel_rejudges_old_not_bigger_skips_once(db_path):
    import kometa.record as rec
    from kometa import marveldc
    sid = db.add_series(title="X", publisher="Marvel", folder_path=None, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    rec.write_issue(sid, 1.0, {"desc": "d", "extra": {"marveldc": {"images": [
        {"url": "u1", "status": "smaller"}, {"url": "u2", "status": "kept"}]}}}, "metron", "full", {"metron_issue_id": 1}, db_path)
    assert marveldc.rejudge_once(db_path) == 1
    imgs = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]["images"]
    assert [i["status"] for i in imgs] == ["pending", "kept"]
    assert marveldc.rejudge_once(db_path) == 0


def test_a_footer_strip_is_not_the_same_image(store):
    # Marvel.com's own copy of some runs: the cover, plus a white 'MARVEL.com' strip at the foot
    from PIL import Image, ImageDraw
    import io
    base = Image.open(io.BytesIO(_jpeg(11, (600, 900)))).convert("RGB")
    footed = base.copy()
    ImageDraw.Draw(footed).rectangle([0, 860, 600, 900], fill=(250, 250, 250))
    ImageDraw.Draw(footed).rectangle([420, 868, 590, 892], fill=(200, 20, 20))

    def jpg(im, size):
        b = io.BytesIO(); im.resize(size).save(b, "JPEG", quality=90); return b.getvalue()
    _put(store, "issue/11/1/file", jpg(base, (300, 450)), "file")
    big = jpg(footed, (1200, 1800))
    _put(store, "issue/11/1/marveldc", big, "marveldc")
    assert coverupgrade.link(11, 1.0, "issue/11/1/marveldc", big, store) == []


def test_relink_once_clears_old_links_and_the_first_table(store):
    import kometa.db as kdb
    coverupgrade.ensure_tables(store)
    with kdb._connect(store) as conn:
        conn.execute("CREATE TABLE image_upgrade (key TEXT)")
        conn.execute("INSERT INTO cover_upgrade (key, better, pixels) VALUES ('issue/1/1/file', 'issue/1/1/marveldc', 9)")
        conn.execute("INSERT INTO cover_upgrade_seen (better) VALUES ('issue/1/1/marveldc')")
    assert coverupgrade.relink_once(store) and not coverupgrade.relink_once(store)
    with kdb._connect(store) as conn:
        assert conn.execute("SELECT count(*) FROM cover_upgrade").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'image_upgrade'").fetchone()[0] == 0


def test_variants_large_view_gets_the_linked_bigger_copy(store, monkeypatch):
    from kometa import main, record
    import kometa.db as kdb
    small, big = _jpeg(12, (300, 450)), _jpeg(12, (1200, 1800))
    _put(store, "issue/12/1/variant/B", small, "metron")
    _put(store, "issue/12/1/marveldc/77", big, "marveldc")
    coverupgrade.link(12, 1.0, "issue/12/1/marveldc/77", big, store)
    monkeypatch.setattr(main, "DB_PATH", store)
    monkeypatch.setattr(kdb, "get_issues_for_series", lambda sid, p: [{"number": 1.0, "metron_issue_id": 5, "tracked_series_id": sid}])
    monkeypatch.setattr(kdb, "get_variant_prefs", lambda *a: None)
    monkeypatch.setattr(record, "variants", lambda issue, p: {"covers": [{"id": "B", "thumb": "t", "large": "l"},
                                                                      {"id": "C", "thumb": "t2", "large": "l2"}]})
    covers = main.get_issue_variants(12, 1.0)["covers"]
    assert covers[0]["big"] == "/api/cover-upgrade/issue/12/1/variant/B" and "big" not in covers[1]
    assert main.get_cover_upgrade("issue/12/1/variant/B").body == big


def test_marvel_print_size_variants_get_their_own_entries(store, monkeypatch):
    import json
    import kometa.db as kdb
    from kometa import main, record
    record.ensure_tables(store)
    sid = kdb.add_series(title="Midnight", publisher="Marvel", folder_path="/comics/m", path=store)
    big, small, exact = _jpeg(20, (1821, 2800)), _jpeg(21, (553, 850)), _jpeg(22, (1821, 2800))
    _put(store, f"issue/{sid}/1/marveldc/138065", big, "marveldc")
    _put(store, f"issue/{sid}/1/marveldc/1", small, "marveldc")
    _put(store, f"issue/{sid}/1/marveldc/2", exact, "marveldc")
    _put(store, f"issue/{sid}/1/variant/B", _jpeg(22, (600, 922)), "metron")
    coverupgrade.link(sid, 1.0, f"issue/{sid}/1/marveldc/2", exact, store)     # already shows as B's big
    imgs = [{"key": f"issue/{sid}/1/marveldc/138065", "kind": "variant", "status": "kept", "width": 1821, "url": "https://cdn.marvel.com/a.jpg"},
            {"key": f"issue/{sid}/1/marveldc/1", "kind": "variant", "status": "kept", "width": 553, "url": "https://cdn.marvel.com/b.jpg"},
            {"key": f"issue/{sid}/1/marveldc/2", "kind": "variant", "status": "kept", "width": 1821, "url": "https://cdn.marvel.com/c.jpg"},
            {"key": f"issue/{sid}/1/marveldc/3", "kind": "variant", "status": "pending", "width": 1821, "url": "https://cdn.marvel.com/d.jpg"}]
    with kdb._connect(store) as conn:
        conn.execute("INSERT INTO issue_record (tracked_series_id, number, extra_json) VALUES (?, 1, ?)",
                     (sid, json.dumps({"marveldc": {"images": imgs}})))
    got = coverupgrade.own_entries(sid, 1.0, store)
    assert [g["id"] for g in got] == ["marvel-138065"]
    assert got[0]["large"] == "https://cdn.marvel.com/a.jpg" and got[0]["thumb"].endswith("?w=400")
    monkeypatch.setattr(main, "DB_PATH", store)
    full = main.get_cover_store(f"issue/{sid}/1/marveldc/138065")
    assert full.body == big
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(main.get_cover_store(f"issue/{sid}/1/marveldc/138065", w=400).body)).size[0] == 400
    import pytest as _pt
    from fastapi import HTTPException
    with _pt.raises(HTTPException):
        main.get_cover_store(f"issue/{sid}/1/variant/B")          # not a Marvel key: not served here
