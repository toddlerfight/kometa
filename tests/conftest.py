"""Shared fixtures. Everything runs against a throwaway SQLite file — no live
DB, no external APIs, no NAS. Pass `path=db_path` to every db call; for the
modules that read a module-level DB_PATH (acquisition), monkeypatch it.
"""
import pytest

import kometa.db as db


@pytest.fixture
def db_path(tmp_path):
    """A fresh, migrated Kometa DB on disk. Torn down with tmp_path."""
    p = str(tmp_path / "kometa_test.db")
    db.init_db(p)
    return p


@pytest.fixture
def series(db_path):
    """A single tracked, monitored series. Returns its id."""
    return db.add_series(
        komga_series_id=None, title="Saga",
        publisher="Image", year_began=2012, folder_path=None,
        on_pull_list=True, path=db_path,
    )
