"""Single-file series report (kometa/singles.py): Metron type first, name second,
split minis spotted by their siblings — and nothing is changed."""
import os

import pytest

import kometa.db as db
import kometa.singles as sg


def _s(title, metron_type=None, metron_series_id=None):
    return {"title": title, "metron_type": metron_type, "metron_series_id": metron_series_id}


def test_metron_type_decides_when_known():
    assert sg.classify(_s("Batman - Lost", "One-Shot"), "Batman - Lost #001 (2017).cbz")["kind"] == "one_shot"
    c = sg.classify(_s("Batman - Hush", "Trade Paperback"), "Batman - Hush (2003).cbz")
    assert c["kind"] == "collected" and c["parent_guess"] == "Batman"


def test_name_decides_when_metron_is_silent():
    c = sg.classify(_s("Batman - The Dark Knight Returns 10th Anniversary Edition"), "DKR.cbz")
    assert c["kind"] == "collected" and "collected edition" in c["why"] and c["parent_guess"] == "Batman"
    c = sg.classify(_s("Batman - Year One"), "Batman - Year One v01 - TPB.cbz")
    assert c["kind"] == "collected"                                   # 'TPB' in the file name
    c = sg.classify(_s("Batman - Year One"), "Batman - Year One.cbz")
    assert c["kind"] == "collected" and "no issue number" in c["why"]
    c = sg.classify(_s("Tintin VS Batman"), "Tintin VS Batman #001.cbz")
    assert c["kind"] == "unknown" and "no catalogue match" in c["why"]
    c = sg.classify(_s("Absolute Batman - Ark M", None, metron_series_id=9), "Absolute Batman - Ark M #001 (2025).cbz")
    assert c["kind"] == "unknown" and "not known yet" in c["why"]


def test_split_minis_need_siblings():
    from kometa.naming import norm_key
    sibs = {norm_key("Batman & Grendel"): ["Batman & Grendel 01 - Devil's Riddle", "Batman & Grendel 02 - Devil's Masque"]}
    c = sg.classify(_s("Batman & Grendel 01 - Devil's Riddle"), "Batman & Grendel 01.cbz", sibs)
    assert c["kind"] == "split" and c["split_base"] == "Batman & Grendel"
    alone = sg.classify(_s("Batman & Grendel 01 - Devil's Riddle"), "Batman & Grendel 01.cbz", {})
    assert alone["kind"] != "split"


def test_report_counts_only_single_file_folders(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(sg, "DB_PATH", db_path)
    root = tmp_path / "comics" / "DC Comics"
    for name, files in {"Batman - Hush": ["Batman - Hush.cbz"], "Batman (2016)": ["Batman #001.cbz", "Batman #002.cbz"],
                        "Batman & Grendel 01 - Devil's Riddle": ["BG01.cbz"], "Batman & Grendel 02 - Devil's Masque": ["BG02.cbz"]}.items():
        d = root / name
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(b"PK")
        sid = db.add_series(title=name, publisher="DC Comics", folder_path=str(d), on_pull_list=False, path=db_path)
        if name == "Batman - Hush":
            db.set_metron_series_id(sid, 1, db_path)
            db.set_metron_type(sid, "Trade Paperback", db_path)
    r = sg.report(db_path)
    assert r["total_series"] == 4 and r["singles"] == 3
    assert r["counts"] == {"one_shot": 0, "collected": 1, "split": 2, "unknown": 0}
    hush = next(x for x in r["rows"] if x["title"] == "Batman - Hush")
    assert hush["parent_guess"] == "Batman" and hush["parent_on_shelf"]          # 'Batman (2016)' is there
    assert r["types_known"] == 1 and r["types_pending"] == 0
    assert all(os.path.isdir(x) for x in [str(root / n) for n in ("Batman - Hush", "Batman (2016)")])   # nothing moved
