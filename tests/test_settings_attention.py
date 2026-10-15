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


def test_locg_never_nags_about_a_pass(wired, monkeypatch):
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 12})
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


def test_fandom_never_nags_about_a_pass(wired, monkeypatch):
    from kometa import fandom_covers
    monkeypatch.setattr(fandom_covers, "status", lambda path=None: {"covers": 10, "downloaded": 4})
    db.set_config({"fandom_download": "1"}, wired)
    assert "fandom" not in _cards(main.settings_attention())


# --- what only a code change can fix: counted, worded as a hand-off --------------------
def test_a_failed_or_stale_backup_is_a_hand_off(wired):
    import json
    assert "backup" not in _cards(main.settings_attention())
    db.set_config({"backup_last": json.dumps({"started_at": "2026-10-01T17:30:00Z", "ok": True})}, wired)
    item = next(i for i in main.settings_attention() if i["card"] == "backup")
    assert item["level"] == "broken" and "didn't run" in item["reason"] and "2 oct" in item["reason"] and "tell Claude" in item["fix"]
    db.set_config({"backup_last": json.dumps({"started_at": "2026-10-01T17:30:00Z", "ok": False, "error": "disk full"})}, wired)
    item = next(i for i in main.settings_attention() if i["card"] == "backup")
    assert "failed: disk full" in item["reason"] and "2 oct" in item["reason"]


def test_a_source_failing_for_a_day_is_broken_but_a_refusal_waits():
    assert main._attn_fix("prh", "error", dry_day=True)[0] == "broken"
    assert main._attn_fix("prh", "refused", dry_day=True)[0] == "wait"
    assert main._attn_fix("prh", "error", dry_day=False)[0] == "wait"


# --- harvesters: grouped, and the non-fatal ones can be dismissed ---------------------------
def test_harvester_trouble_is_grouped_and_dismissable_until_it_clears(wired, monkeypatch):
    from kometa import health
    bad = [{"card": "prh", "kind": "error", "reason": "PRH: failing", "dry_day": True}]
    monkeypatch.setattr(health, "unwell", lambda path=None: bad)
    item = next(i for i in main.settings_attention() if i["card"] == "prh")
    assert item["group"] == "harvesters" and item["level"] == "broken" and item["dismissable"] is True
    main.dismiss_attention(main.DismissRequest(card="prh", level="broken"))
    assert "prh" not in _cards(main.settings_attention())
    monkeypatch.setattr(health, "unwell", lambda path=None: [])          # it cleared
    main.settings_attention()
    assert main._dismissed() == {}                                       # the dismissal is forgotten
    monkeypatch.setattr(health, "unwell", lambda path=None: bad)         # back again: shown again
    assert "prh" in _cards(main.settings_attention())


def test_what_is_yours_to_fix_cannot_be_dismissed(wired):
    db.set_config({"hardcover_token": ""}, wired)
    item = next(i for i in main.settings_attention() if i["card"] == "hardcover")
    assert item["level"] == "fix" and item["dismissable"] is False
    with pytest.raises(Exception):
        main.dismiss_attention(main.DismissRequest(card="hardcover", level="fix"))
    assert "hardcover" in _cards(main.settings_attention())
