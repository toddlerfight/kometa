"""Startup pre-warm (related_combined.prewarm): the index is built in the background
when the combined engine is in use, so the first Related row after a deploy is served
from it; with another engine nothing is built."""
import pytest

import kometa.db as db
import kometa.related_combined as rc


@pytest.fixture(autouse=True)
def _fresh_index():
    rc._cache.update(ix=None, path=None, fp=None, at=0.0, opts=None)
    yield


def test_the_combined_engine_is_warm_before_the_first_row(db_path, monkeypatch):
    db.set_config({"related_engine": "combined"}, path=db_path)
    t = rc.prewarm(db_path)
    t.join(10)
    assert rc._cache["ix"] is not None and rc._cache["path"] == db_path
    monkeypatch.setattr(rc, "build_index", lambda *a, **k: pytest.fail("the first row rebuilt the index"))
    rc.index(db_path)


def test_another_engine_builds_nothing(db_path, monkeypatch):
    monkeypatch.setattr(rc, "build_index", lambda *a, **k: pytest.fail("built for an engine not in use"))
    assert rc.prewarm(db_path) is None and rc._cache["ix"] is None
