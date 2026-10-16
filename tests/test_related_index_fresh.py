"""A series the warm Related index knows nothing about gets a fresh look within
FRESH_RECHECK_S, not INDEX_TTL_S (Absolute Evil, 2026-10-17: tracked a minute after
a restart built the index, its credits landed at 06:23 and its row stayed empty to 06:26)."""
import pytest

import kometa.db as db
import kometa.record as rec
import kometa.related as rel
import kometa.related_combined as rc


@pytest.fixture(autouse=True)
def _fresh_index():
    rc._cache.update(ix=None, path=None, fp=None, at=0.0, opts=None)
    rel._nb_cache.update(value=None, path=None, at=0.0)
    yield


def _run(db_path, title, writer=None):
    sid = db.add_series(title=title, publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    if writer:
        _credit(db_path, sid, writer)
    return sid


def _credit(db_path, sid, writer):
    for n in (1.0, 2.0):
        db.upsert_issue_status(sid, n, "2025-01-01", 1, path=db_path)
        rec.write_issue(sid, n, {"credits": [{"role": "writer", "name": writer}]}, "metron", "full",
                        {"metron_issue_id": sid * 1000 + int(n)}, db_path)


def test_a_new_series_row_fills_once_its_credits_land_without_waiting_out_the_ttl(db_path, monkeypatch):
    monkeypatch.setattr(rc, "FRESH_RECHECK_S", 0)
    green = _run(db_path, "Absolute Green Lantern", "Al Ewing")
    other = _run(db_path, "Other", "Someone")
    evil = _run(db_path, "Absolute Evil")                       # just tracked: nothing yet
    builds = []
    real = rc.build_index
    monkeypatch.setattr(rc, "build_index", lambda *a, **k: builds.append(1) or real(*a, **k))

    assert rc.related_combined(evil, path=db_path) == []
    _credit(db_path, evil, "Al Ewing")                          # the fill lands
    row = rc.related_combined(evil, path=db_path)
    assert [c["series_id"] for c in row] == [green] and len(builds) == 2

    # a series the index already knows waits out the usual TTL
    _credit(db_path, other, "Al Ewing")
    rc.related_combined(green, path=db_path)
    assert len(builds) == 2
