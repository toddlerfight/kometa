"""Cover check (kometa/coververify.py): the cover, not the file name, says which
issue a file is. No network — catalogue fetches are stubbed."""
import io

import kometa.db as db
from kometa import coververify as cv


def _img(seed: int) -> bytes:
    """A distinct 'cover' per seed: random blocks, so two seeds share nothing."""
    import random
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("L", (90, 140))
    px = im.load()
    blocks = {(bx, by): rnd.randrange(256) for bx in range(9) for by in range(10)}
    for x in range(90):
        for y in range(140):
            px[x, y] = blocks[(x // 10, y // 14)]
    b = io.BytesIO(); im.convert("RGB").save(b, "JPEG", quality=90)
    return b.getvalue()


def _file_cover(tmp_path, db_path, sid, n, data):
    p = tmp_path / f"f{n:g}.jpg"; p.write_bytes(data)
    from kometa import images
    images.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO image_record (key, path, url, bytes, fetched_at, source) VALUES (?, ?, NULL, ?, '', 'file')",
                     (f"issue/{sid}/{n:g}/file", str(p), len(data)))


def test_same_picture_resized_is_close_different_covers_are_far():
    from PIL import Image
    a = _img(1)
    with Image.open(io.BytesIO(a)) as im:
        b = io.BytesIO(); im.resize((300, 466)).save(b, "JPEG", quality=60)
    assert cv.hamming(cv.dhash(a), cv.dhash(b.getvalue())) <= cv.CLOSE
    assert cv.hamming(cv.dhash(_img(1)), cv.dhash(_img(2))) > cv.CLOSE


def test_match_misnumbered_and_unclear(tmp_path, db_path):
    sid = db.add_series(title="Uncanny X-Men", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    for n in (1.0, 2.0, 3.0):
        db.upsert_issue_status(sid, n, None, 1, metron_image=f"https://cat/{n:g}.jpg", path=db_path)
    _file_cover(tmp_path, db_path, sid, 1.0, _img(1))     # right
    _file_cover(tmp_path, db_path, sid, 2.0, _img(3))     # it's really #3
    _file_cover(tmp_path, db_path, sid, 3.0, _img(9))     # nobody's cover (a variant?)
    covers = {f"https://cat/{n}.jpg": _img(n) for n in (1, 2, 3)}
    r = cv.verify(sid, path=db_path, fetch=lambda u: covers[u], sleep=lambda s: None)
    by = {row["number"]: row for row in r["rows"]}
    assert by[1.0]["status"] == "match"
    assert by[2.0]["status"] == "misnumbered" and by[2.0]["looks_like"] == 3.0
    assert by[3.0]["status"] == "unclear"
    assert r["summary"] == {"match": 1, "misnumbered": 1, "unclear": 1}
