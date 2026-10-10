"""Variants into the record (kometa/record.py): typed names, Metron then LOCG, the tab reads the record."""
import kometa.db as db
import kometa.record as rec


def test_variant_type_reads_the_name_conservatively():
    cases = {
        "Cover A (Main)": "cover", "Cover B Jock Variant": "cover", "1:25 Jae Lee Variant": "ratio",
        "Incentive Cover D": "ratio", "1 in 50 Virgin Variant": "ratio", "Virgin Variant Cover": "virgin",
        "Forbidden Planet Exclusive": "store exclusive", "NYCC 2024 Variant": "convention",
        "Sketch Cover": "sketch", "B&W Variant": "sketch", "2nd Printing": "reprint", "Foil Variant": "foil", "": "cover", None: "cover",
    }
    for name, want in cases.items():
        assert rec.variant_type(name) == want, name


def _issue(db_path, metron=True, locg=True):
    sid = db.add_series(title="White Knight", publisher="DC", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2017-10-04", 1, metron_issue_id=(900 if metron else None),
                           locg_issue_id=("77" if locg else None), path=db_path)
    return sid


METRON = {"desc": "", "credits": [], "arcs": [], "covers": [{"id": "m900", "name": "Cover A (Main)", "thumb": "https://m/a.jpg", "large": "https://m/a.jpg"},
                                                            {"id": "v1", "name": "1:25 Variant", "thumb": "https://m/b.jpg", "large": "https://m/b.jpg"}]}
LOCG = [{"id": "77", "name": "Cover A (Main)", "thumb": "https://s3/77.jpg", "large": "https://s3/77l.jpg"},
        {"id": "78", "name": "Forbidden Planet Exclusive", "thumb": "https://s3/78.jpg", "large": "https://s3/78l.jpg"},
        {"id": "79", "name": "1:25 Variant", "thumb": "https://s3/79.jpg", "large": "https://s3/79l.jpg"}]


def test_metron_first_then_locg_merged_by_name_only_when_open(db_path):
    sid = _issue(db_path)
    asked = []
    m = lambda issue, path: (asked.append("metron") or METRON)
    l = lambda lid: (asked.append("locg") or LOCG)
    covers = rec.fill_variants(sid, 1.0, db_path, metron=m, locg=l, locg_open=lambda: False)
    assert [c["name"] for c in covers] == ["Cover A (Main)", "1:25 Variant"] and covers[1]["type"] == "ratio"
    assert asked == ["metron"]
    import kometa.topup as tp
    assert tp.waiting(db_path)["by_kind"] == {"variants": 1}                      # queued for the next pass
    covers = rec.fill_variants(sid, 1.0, db_path, force=True, metron=m, locg=l, locg_open=lambda: True)
    assert [c["name"] for c in covers] == ["Cover A (Main)", "1:25 Variant", "Forbidden Planet Exclusive"]
    assert covers[2]["source"] == "locg" and covers[2]["type"] == "store exclusive" and covers[2]["large"] == "https://s3/78l.jpg"
    row = rec.get_issue(sid, 1.0, db_path)
    assert row["variants_at"] and row["fill_state"] == "variants"   # covers only: details still owed and len(row["covers"]) == 3
    # fresh: not asked again; a settled issue (2017) is good for 30 days
    assert rec.fill_variants(sid, 1.0, db_path, metron=m, locg=l, locg_open=lambda: True) == covers and asked == ["metron", "metron", "locg"]


def test_the_tab_reads_the_record_and_fills_it_when_empty(db_path, monkeypatch):
    sid = _issue(db_path, locg=False)
    monkeypatch.setattr("kometa.issue_meta._metron", lambda issue, path: METRON)
    issue = next(i for i in db.get_issues_for_series(sid, db_path) if i["number"] == 1.0)
    first = rec.variants(issue, db_path)
    assert first["record"] is False and [c["type"] for c in first["covers"]] == ["cover", "ratio"]
    second = rec.variants(issue, db_path)
    assert second["record"] is True and second["covers"] == first["covers"]
