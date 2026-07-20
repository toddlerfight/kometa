import json
import time

import pytest

from kometa import version as v


@pytest.fixture
def stamp(tmp_path, monkeypatch):
    """Point the module at a throwaway stamp file."""
    p = tmp_path / "_build.json"
    monkeypatch.setattr(v, "_STAMP", p)
    return p


def test_reads_the_stamp_when_present(stamp):
    stamp.write_text(json.dumps({
        "sha": "a" * 40, "dirty": False, "branch": "main", "deployed_at": 1,
    }))
    info = v.build_info()
    assert info["sha"] == "a" * 40
    assert info["short_sha"] == "aaaaaaa"
    assert info["source"] == "stamp"
    assert info["branch"] == "main"


def test_falls_back_to_git_when_no_stamp(stamp):
    # stamp file does not exist; this repo IS a git checkout, so git wins
    info = v.build_info()
    assert info["source"] == "git"
    assert info["sha"] and len(info["sha"]) == 40
    assert info["short_sha"] == info["sha"][:7]


def test_unknown_when_stamp_is_junk(stamp, monkeypatch):
    stamp.write_text("not json at all")
    monkeypatch.setattr(v, "_from_git", lambda: None)
    info = v.build_info()
    assert info["source"] == "unknown"
    assert info["sha"] is None


def test_stamp_missing_sha_is_rejected(stamp, monkeypatch):
    """A stamp without a sha is worse than no stamp — it must not be trusted."""
    stamp.write_text(json.dumps({"branch": "main", "deployed_at": 1}))
    monkeypatch.setattr(v, "_from_git", lambda: None)
    assert v.build_info()["source"] == "unknown"


def test_flags_when_files_are_newer_than_the_process(stamp):
    """Synced but never restarted — the silent one."""
    stamp.write_text(json.dumps({
        "sha": "b" * 40, "deployed_at": v._PROCESS_STARTED + 500,
    }))
    assert v.build_info()["restart_may_be_needed"] is True


def test_no_flag_when_deploy_predates_the_process(stamp):
    stamp.write_text(json.dumps({
        "sha": "b" * 40, "deployed_at": v._PROCESS_STARTED - 500,
    }))
    assert v.build_info()["restart_may_be_needed"] is False


def test_not_cached_so_drift_is_visible(stamp):
    """A cached answer would hide the exact thing this module reports."""
    stamp.write_text(json.dumps({"sha": "c" * 40, "deployed_at": 1}))
    assert v.build_info()["short_sha"] == "ccccccc"
    stamp.write_text(json.dumps({"sha": "d" * 40, "deployed_at": 2}))
    assert v.build_info()["short_sha"] == "ddddddd"


def test_process_started_is_sane(stamp):
    info = v.build_info()
    assert info["process_started"] <= time.time()
