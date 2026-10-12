"""Settings badge (main.settings_attention) and the LOCG off switch. No network."""
import time

import pytest

import kometa.db as db
from kometa import locg_client, main, metron_client


@pytest.fixture
def wired(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    root = tmp_path / "comics"
    root.mkdir()
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(root))
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 0})
    # a fully set-up house: nothing should nag
    db.set_config({"metron_token": "t", "cv_api_key": "k", "gcd_user": "u", "gcd_pass": "p",
                   "prowlarr_url": "http://p", "prowlarr_apikey": "k", "sab_url": "http://s", "sab_apikey": "k",
                   "qbit_url": "http://q", "qbit_user": "u", "hardcover_token": "h"}, dbp)
    return dbp


def _cards(items):
    return {i["card"] for i in items}


def test_a_set_up_house_has_nothing_to_say(wired):
    assert main.settings_attention() == []


def test_missing_logins_light_their_cards(wired):
    db.set_config({"cv_api_key": "", "gcd_pass": ""}, wired)
    assert _cards(main.settings_attention()) == {"comicvine", "gcd"}


def test_a_switched_off_source_never_nags(wired):
    db.set_config({"cv_api_key": "", "comicvine_enabled": "0"}, wired)
    assert "comicvine" not in _cards(main.settings_attention())


def test_metron_refusal_lights_the_card(wired):
    creds = metron_client._creds()
    db.set_config({metron_client.AUTH_REFUSED_KEY: f"{metron_client._creds_fp(creds)}|{time.time():.0f}"}, wired)
    items = main.settings_attention()
    assert any(i["card"] == "metron" and "refused" in i["reason"] for i in items)


def test_locg_waiting_without_a_pass(wired, monkeypatch):
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 12})
    assert "locg" in _cards(main.settings_attention())
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": True, "locg_topup_waiting": 12})
    assert "locg" not in _cards(main.settings_attention())


def test_missing_comics_folder(wired, monkeypatch):
    monkeypatch.setattr(main, "_comics_root", lambda: "/nonexistent-kometa-root")
    assert "root" in _cards(main.settings_attention())


class TestLocgSwitch:
    def test_off_means_not_one_request(self, wired):
        db.set_config({"locg_enabled": "0"}, wired)
        with pytest.raises(locg_client.LocgPaused, match="switched off"):
            locg_client._check_paused()
        assert locg_client.locg_paused() is not None          # every caller sees LOCG as shut
        assert locg_client.pause_label(time.time()) == "switched off"

    def test_on_by_default(self, wired, monkeypatch):
        monkeypatch.setattr(locg_client, "_paused_until", lambda: 0.0)
        locg_client._check_paused()                           # no raise
        assert locg_client.locg_paused() is None

    def test_config_exposes_the_flag(self, wired):
        assert main.get_config()["locg_enabled"] is True
        db.set_config({"locg_enabled": "0"}, wired)
        assert main.get_config()["locg_enabled"] is False


# --- Komga retired 2026-10-12: no config keys, no routes, no disconnect ----------
def test_config_carries_no_komga_keys(wired):
    # even on a DB that still holds the old rows
    db.set_config({"komga_url": "http://komga:8585", "komga_user": "u", "komga_pass": "p",
                   "komga_enabled": "1", "komga_covers": "1"}, wired)
    assert not [k for k in main.get_config() if "komga" in k]


def test_config_patch_ignores_komga_keys(wired):
    main.update_config(main.ConfigRequest(**{"komga_url": "http://new:8585", "sync_hours": "6"}))
    cfg = db.get_config(wired)
    assert cfg.get("sync_hours") == "6" and "komga_url" not in cfg


def test_no_komga_routes_or_disconnect(wired):
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert not [p for p in paths if "komga" in p.lower()]
    with pytest.raises(main.HTTPException):
        main.disconnect_integration("komga")
