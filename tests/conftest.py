"""Shared fixtures. Everything runs against a throwaway SQLite file — no live
DB, no external APIs, no NAS. Pass `path=db_path` to every db call; for the
modules that read a module-level DB_PATH (acquisition), monkeypatch it.
"""
import io
import os
import time
import zipfile

# The suite runs on the container's clock: UTC. Kometa's "today" comes from Python's
# date.today() in some places and SQLite's date('now') (always UTC) in others; in the
# container (TZ unset) they agree. On a Mac in Brisbane they differed by a day between
# midnight and 10 am, and three date tests failed every night (2026-10-17). Set before
# any test module computes a date at import.
os.environ["TZ"] = "UTC"
time.tzset()

import pytest  # noqa: E402

import kometa.db as db  # noqa: E402


def make_cbz(path, pages=2):
    """A real, readable CBZ. Ownership opens files now — it refuses to count an
    archive that can't produce a single page — so a 4-byte 'PK\\x03\\x04' stub no
    longer reads as a comic anyone owns. Fixtures have to be honest."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i:03d}.jpg", b"\xff\xd8\xff\xe0 jpeg-ish")
    path.write_bytes(buf.getvalue())
    return path


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
        title="Saga",
        publisher="Image", year_began=2012, folder_path=None,
        on_pull_list=True, path=db_path,
    )


@pytest.fixture(autouse=True)
def _finalize_inline(monkeypatch):
    """Pollers hand finished downloads to a worker thread in the app; tests run
    that work inline so a poll call's effects are visible when it returns."""
    import kometa.acquisition as acquisition
    monkeypatch.setattr(acquisition, "FINALIZE_INLINE", True)


@pytest.fixture(autouse=True)
def _health_in_memory(monkeypatch):
    """kometa/health.py records every request; outside its own tests that stays in
    memory (no writes to whatever DB_PATH happens to be) and starts empty."""
    from kometa import health
    monkeypatch.setattr(health, "PERSIST", False)
    health.reset()
    yield
    health.reset()
