"""Folder paths stay inside the library (sources.check_series_folder / check_comics_root).
Security review 2026-10-11 item 2: any series folder, and the comics root itself, could
be pointed anywhere in the container, and Remove / tidy / placement act on what's there."""
import os

import pytest

import kometa.db as db
import kometa.main as main
import kometa.sources as sources
from kometa.naming import _resolve_dir
from test_auth import TestClient


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "comics"
    (root / "Image" / "Saga").mkdir(parents=True)
    (tmp_path / "comics2" / "Image" / "Saga").mkdir(parents=True)
    return root


@pytest.mark.parametrize("rel", ["Image/Saga", "DC/Batman/Batman (1940)"])
def test_a_series_folder_two_deep_passes(lib, rel):
    assert sources.check_series_folder(str(lib / rel), str(lib)) == str(lib / rel)


@pytest.mark.parametrize("path", ["", "Image", "_trash/Image/Saga", ".kometa-staging/x/y", "../comics2/Image/Saga", "/data/x/y"])
def test_anything_else_is_refused(lib, path):
    with pytest.raises(sources.OutsideLibrary):
        sources.check_series_folder(path if path.startswith("/") else str(lib / path) if path else str(lib), str(lib))


def test_the_comics_root_cant_be_the_system_or_kometas_own(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(db, "DB_PATH", str(data / "kometa.db"))
    for bad in ("/", "/etc", "/var", str(data), str(tmp_path), str(tmp_path / "missing")):
        with pytest.raises(sources.OutsideLibrary):
            sources.check_comics_root(bad)
    (tmp_path / "comics").mkdir()
    assert sources.check_comics_root(str(tmp_path / "comics"))


def test_a_name_of_dots_doesnt_file_a_series_as_its_publisher(lib):
    p = _resolve_dir(str(lib), "...", "..")
    assert os.path.relpath(p, lib).split(os.sep) == ["Unknown", "Untitled"]


@pytest.fixture
def c(tmp_path, lib, monkeypatch):
    import kometa.auth as auth
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    for mod in (main, auth, db):
        monkeypatch.setattr(mod, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(lib))
    monkeypatch.setattr(main, "_rescan_owned", lambda s: None)
    auth.forget_enabled()
    return TestClient(main.app), dbp


def test_the_endpoints_refuse_paths_outside(c, lib, tmp_path):
    client, dbp = c
    sid = db.add_series(title="Saga", publisher="Image", folder_path=str(lib / "Image" / "Saga"), on_pull_list=False, path=dbp)
    r = client.patch(f"/api/series/{sid}/folder", json={"folder_path": str(lib / "Image")})
    assert r.status_code == 422 and db.get_series_by_id(sid, dbp)["folder_path"] == str(lib / "Image" / "Saga")
    assert client.patch(f"/api/series/{sid}/folder", json={"folder_path": str(lib / "Image" / "Saga")}).status_code == 200
    assert client.patch("/api/config", json={"comics_root": "/"}).status_code == 422
    # browse: a sibling that merely starts with the root's name isn't inside it
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        main.browse_fs(path=str(tmp_path / "comics2"), scope="library")
    assert e.value.status_code == 403
    # a new folder outside the library: only while there's no library yet
    assert client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "x", "scope": "fs"}).status_code == 403
    assert not (tmp_path / "x").exists()
