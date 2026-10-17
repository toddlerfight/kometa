"""Trending collected editions (kometa/trending.py fill_collected_covers / shown): each
volume wears its own cover or isn't shown. 2026-10-17: Absolute Batman Vol 1, 2 and 3
all wore the run's cover, Dungeon Crawler Carl Vol 1 and 2 both wore LOCG's series
art, Vol 3 and Vol 3 HC were two cards, and three catalogue misses were blank tiles."""
import kometa.db as db
import kometa.trending as tr


def _gn(title, rank, **kw):
    return dict(tr.parse_title(title), title=title, rank=rank, **kw)


def _trades(sid, rows, path):
    db.set_trades(sid, rows, path=path)


def _no_metron(series, vol):
    return {"results": []}


def test_an_owned_run_shows_each_volume_with_its_own_trade_cover(db_path):
    sid = db.add_series(title="Absolute Batman", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    _trades(sid, [{"vol": 1, "format": "TPB", "cover": "zoo.jpg"}, {"vol": 2, "format": "TPB", "cover": "abom.jpg"},
                  {"vol": 2, "format": "HC", "cover": "abom-hc.jpg"}], db_path)
    rows = [_gn("Absolute Batman Vol. 3 Devil's Workshop", 1), _gn("Absolute Batman Vol. 1 The Zoo", 2),
            _gn("Absolute Batman Vol. 2 Abomination", 4), _gn("Absolute Batman Vol. 2 Abomination HC", 6)]
    tr.match_shelf(rows, db_path)
    assert all(e["cover"] == f"/api/series/{sid}/thumbnail" for e in rows)       # what the row used to wear
    got = tr.shown({"comics": [], "graphic_novels": rows}, db_path)["graphic_novels"]
    # Vol 3 has no trade cover yet: not shown. Vol 2 HC is Vol 2 again: one card, the better rank.
    assert [(e["rank"], e["cover"]) for e in got] == [(2, "zoo.jpg"), (4, "abom.jpg")]


def test_the_chart_format_picks_the_hc_cover(db_path):
    sid = db.add_series(title="Absolute Wonder Woman", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    _trades(sid, [{"vol": 1, "format": "TPB", "cover": "tpb.jpg"}, {"vol": 1, "format": "HC", "cover": "hc.jpg"}], db_path)
    rows = [_gn("Absolute Wonder Woman Vol. 1 The Last Amazon HC", 10)]
    tr.match_shelf(rows, db_path)
    assert tr.shown({"graphic_novels": rows}, db_path)["graphic_novels"][0]["cover"] == "hc.jpg"


def test_metron_gives_the_collected_sibling_not_the_runs_single_issue(db_path):
    rows = [_gn("Absolute Martian Manhunter Vol. 2 The Agency", 3, metron_series_id=77),
            _gn("Absolute Martian Manhunter Vol. 1 Martian Vision", 7, metron_series_id=77)]
    asked = []

    def lookup(series, vol):
        asked.append((series, vol))
        return {"results": [
            {"series": {"name": "Absolute Martian Manhunter (2025)"}, "image": f"single-{vol}.jpg"},
            {"series": {"name": "Absolute Martian Manhunter HC (2025)"}, "image": f"hc-{vol}.jpg"},
            {"series": {"name": "Absolute Martian Manhunter TPB (2026)"}, "image": f"tpb-{vol}.jpg"},
            {"series": {"name": "Absolute Batman TPB (2025)"}, "image": "wrong.jpg"}]}
    data = {"comics": [], "graphic_novels": rows}
    assert tr.fill_collected_covers(data, db_path, lookup=lookup, locg_trades=lambda lid: 1 / 0) == 2
    assert asked == [("Absolute Martian Manhunter", 2), ("Absolute Martian Manhunter", 1)]
    assert [e["cover"] for e in tr.shown(data, db_path)["graphic_novels"]] == ["tpb-2.jpg", "tpb-1.jpg"]
    tr.fill_collected_covers(data, db_path, lookup=lambda s, v: 1 / 0)              # found once: not asked again


def test_a_run_only_locg_knows_takes_each_volume_from_its_edition_list(db_path):
    dcc = dict(catalogue_miss=True, locg_tried=True, locg_series_id=209896, publisher="Vault Comics")
    rows = [_gn("Dungeon Crawler Carl Vol. 2 (Regular Edition Cover)", 6, cover="series.jpg", **dcc),
            _gn("Dungeon Crawler Carl Vol. 1 (Regular Edition)", 9, cover="series.jpg", **dcc),
            _gn("Dungeon Crawler Carl Vol. 4", 12, cover="series.jpg", **dcc)]
    eds = [{"vol": 1, "format": "TPB", "cover": "dcc-1.jpg"}, {"vol": 2, "format": "TPB", "cover": "dcc-2.jpg"}]
    data = {"comics": [], "graphic_novels": rows}
    tr.fill_collected_covers(data, db_path, lookup=_no_metron, locg_trades=lambda lid: eds)
    got = tr.shown(data, db_path)["graphic_novels"]
    assert [(e["rank"], e["cover"]) for e in got] == [(6, "dcc-2.jpg"), (9, "dcc-1.jpg")]   # Vol 4: none of its own
    assert all(e["vol_tried"] for e in rows)


def test_a_locg_that_didnt_answer_is_asked_again(db_path):
    e = _gn("Dungeon Crawler Carl Vol. 1", 9, catalogue_miss=True, locg_tried=True, locg_series_id=209896)
    data = {"comics": [], "graphic_novels": [e]}

    def down(lid):
        raise RuntimeError("paused")
    tr.fill_collected_covers(data, db_path, lookup=_no_metron, locg_trades=down)
    assert not e.get("vol_tried") and e["vol_metron"]
    asked = []
    tr.fill_collected_covers(data, db_path, lookup=lambda s, v: asked.append(s),
                             locg_trades=lambda lid: [{"vol": 1, "format": "TPB", "cover": "dcc-1.jpg"}])
    assert asked == [] and e["vol_cover"] == "dcc-1.jpg"                     # Metron's answer was kept


def test_a_run_not_yet_looked_up_waits_for_locg(db_path):
    e = _gn("Dungeon Crawler Carl Vol. 1", 9, catalogue_miss=True)               # LOCG not asked for the run yet
    data = {"comics": [], "graphic_novels": [e]}
    tr.fill_collected_covers(data, db_path, lookup=_no_metron, locg_trades=lambda lid: 1 / 0)
    assert not e.get("vol_tried")


def test_nothing_shows_without_a_cover_and_no_cover_shows_twice(db_path):
    data = {"comics": [_gn("DNX #1", 4, cover="dnx.jpg"), _gn("Mystery #1", 5), _gn("Echo #2", 6, cover="dnx.jpg")],
            "graphic_novels": [_gn("Marvel Dimensions HC", 8, catalogue_miss=True),
                               _gn("DC Compact Edition Vol. 22 Batman The Long Halloween", 12, catalogue_miss=True, cover="run.jpg"),
                               _gn("Monstress Book One", 13, cover="monstress.jpg")]}
    got = tr.shown(data, db_path)
    assert [e["rank"] for e in got["comics"]] == [4]
    assert [e["rank"] for e in got["graphic_novels"]] == [13]


def test_the_endpoint_serves_the_shown_rows_and_keeps_the_record(db_path, monkeypatch):
    sid = db.add_series(title="Absolute Batman", publisher="DC Comics", folder_path=None, on_pull_list=True, path=db_path)
    _trades(sid, [{"vol": 1, "format": "TPB", "cover": "zoo.jpg"}], db_path)
    data = {"source": "ICv2", "month": "September 2026", "comics": [],
            "graphic_novels": [_gn("Absolute Batman Vol. 1 The Zoo", 2), _gn("Marvel Dimensions HC", 8, catalogue_miss=True)]}
    tr._save(data, db_path)
    monkeypatch.setattr(tr, "_enrich_in_background", lambda d, p: None)
    got = tr.trending(path=db_path)
    assert [(e["rank"], e["cover"]) for e in got["graphic_novels"]] == [(2, "zoo.jpg")] and not got["pending"]
    assert len(tr._load(db_path)["graphic_novels"]) == 2                     # hidden, not forgotten
