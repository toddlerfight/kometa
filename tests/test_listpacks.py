"""Reading-list packs (kometa/listpacks.py): chosen on what's inside, filed by run."""
import os
import shutil

import pytest

import kometa.db as db
import kometa.listget as lg
import kometa.listpacks as lp
import kometa.readlists as rl


def _benc(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(_benc(v) for v in x) + b"e"
    return b"d" + b"".join(_benc(k) + _benc(v) for k, v in sorted(x.items())) + b"e"


def _torrent(names):
    return _benc({"announce": "x", "info": {"name": "Secret Wars (Story Arc)", "piece length": 1,
                                             "files": [{"length": 1, "path": ["Secret Wars (Story Arc)", n]} for n in names]}})


NZB = """<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
<file subject='Old Man Logan [1/5] - "Old Man Logan 001 (2015) (Digital).cbz" yEnc'/>
<file subject='Old Man Logan [2/5] - "Old Man Logan 002 (2015) (Digital).cbz" yEnc'/>
<file subject='Old Man Logan [3/5] - "Old Man Logan.nfo" yEnc'/></nzb>"""


@pytest.fixture
def swlist(db_path):
    items = [{"series": "Secret Wars", "number": str(n), "volume": "2015"} for n in range(1, 10)]
    items += [{"series": "Secret Wars 2099", "number": str(n), "volume": "2015"} for n in range(1, 6)]
    items += [{"series": "Old Man Logan", "number": str(n), "volume": "2015"} for n in range(1, 6)]
    return rl.save_list("Secret Wars (2015): Battleworld", items, "cbl", None, db_path)


def test_a_file_fills_the_gap_of_the_longest_series_that_leads_its_name(swlist, db_path):
    gaps = lp.gap_index(swlist, db_path)
    sw2099 = gaps[lp._key("Secret Wars 2099")]["numbers"][1.0]
    sw = gaps[lp._key("Secret Wars")]["numbers"][1.0]
    assert lp.match_file("Secret Wars 2099 001 (2015) (Digital) (Zone-Empire).cbz", gaps) == sw2099
    assert lp.match_file("Secret Wars 01 (of 09) (2015) (9 covers).cbz", gaps) == sw
    assert lp.match_file("Secret Wars Journal 001 (2015).cbz", gaps) is None          # not on this list's gaps
    assert lp.match_file("Secret Wars 10 (2016).cbz", gaps) is None


def test_file_lists_come_from_the_torrent_and_the_nzb_not_the_comics():
    assert lp.torrent_files(_torrent(["Secret Wars 001 (2015).cbz", "Thors 001 (2015).cbr"])) == [
        "Secret Wars (Story Arc)/Secret Wars 001 (2015).cbz", "Secret Wars (Story Arc)/Thors 001 (2015).cbr"]
    assert lp.nzb_files(NZB) == ["Old Man Logan 001 (2015) (Digital).cbz", "Old Man Logan 002 (2015) (Digital).cbz", "Old Man Logan.nfo"]


class _Prowlarr:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def search(self, q, protocol=None, limit=100, categories=None):
        self.asked.append((q, categories))
        return self.rows


def test_the_hunt_judges_each_pack_on_its_file_list_and_skips_editions_and_films(swlist, db_path):
    rows = [
        {"title": "Secret Wars (Story Arc) (2015-2016)", "protocol": "torrent", "url": "http://prowlarr/t1", "magnet": "", "size": 10e9, "seeders": 8},
        {"title": "Old Man Logan (2015) 01-05", "protocol": "usenet", "url": "http://prowlarr/n1", "magnet": "", "size": 3e8, "seeders": 0},
        {"title": "Secret Wars Omnibus (2017) (Digital)", "protocol": "usenet", "url": "http://prowlarr/n2", "size": 5e9},
        {"title": "Secret Wars 2015 1080p WEBRip x264", "protocol": "torrent", "url": "http://prowlarr/t2", "size": 2e9},
    ]
    fetched = []
    def fetch(url):
        fetched.append(url)
        if url == "http://prowlarr/t1":
            return _torrent([f"Secret Wars {n:02d} (of 09) (2015).cbz" for n in range(1, 10)]
                            + [f"Secret Wars 2099 {n:03d} (2015).cbz" for n in range(1, 6)]), ""
        return NZB.encode(), ""
    pr = _Prowlarr(rows)
    hunt = lp.find_packs(swlist, db_path, prowlarr=pr, fetch=fetch)
    assert set(fetched) == {"http://prowlarr/t1", "http://prowlarr/n1"}          # the omnibus and the film never read
    by = {c["title"]: c["covered"] for c in hunt["candidates"]}
    assert by == {"Secret Wars (Story Arc) (2015-2016)": 14, "Old Man Logan (2015) 01-05": 2}
    assert [c["title"] for c in hunt["chosen"]] == ["Secret Wars (Story Arc) (2015-2016)"]   # OML fills 2: under the bar


def test_choose_never_counts_a_gap_twice():
    a = {"title": "A", "covers": list(range(10)), "seeders": 1}
    b = {"title": "B", "covers": list(range(5, 12)), "seeders": 9}
    picked = lp.choose([a, b], 20)
    assert [p["title"] for p in picked] == ["A"]                                # B's new gaps (10, 11) are under the bar
    assert picked[0]["fills"] == list(range(10))


def test_place_files_each_match_in_its_runs_folder_and_leaves_the_rest(swlist, db_path, tmp_path, monkeypatch):
    root = tmp_path / "comics" / "Marvel Comics"
    sw = db.add_series(title="Secret Wars", publisher="Marvel", year_began=2015, folder_path=str(root / "Secret Wars"), on_pull_list=False, path=db_path)
    oml = db.add_series(title="Old Man Logan", publisher="Marvel", year_began=2015, folder_path=str(root / "Old Man Logan"), on_pull_list=False, path=db_path)
    dl = tmp_path / "dl"
    dl.mkdir()
    names = ["Secret Wars 01 (of 09) (2015).cbz", "Old Man Logan 002 (2015).cbz", "Thors 001 (2015).cbz"]
    for n in names:
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "tiny.cbz"), dl / n) if os.path.exists(
            os.path.join(os.path.dirname(__file__), "fixtures", "tiny.cbz")) else (dl / n).write_bytes(b"PK\x05\x06" + b"\0" * 18)
    gaps = lp.gap_index(swlist, db_path)
    fills = [gaps[lp._key("Secret Wars")]["numbers"][1.0], gaps[lp._key("Old Man Logan")]["numbers"][2.0]]
    db.queue_issue(sw, 1.0, db_path)
    with db._connect(db_path) as c:
        qid = c.execute("INSERT INTO download_queue (tracked_series_id, issue_number, kind, state, meta_json) VALUES (?, ?, 'list_pack', 'processing', ?)",
                        (sw, lp.pack_issue_number(swlist, 0), '{"list_id": %d, "fills": %s}' % (swlist, fills))).lastrowid
    runs = {lp._key("Secret Wars"): sw, lp._key("Old Man Logan"): oml}
    def ensure(list_id, item_id, path):
        e = next(x for x in rl.resolve(list_id, path)["entries"] if x["item_id"] == item_id)
        return db.get_series_by_id(runs[lp._key(e["series"])], path)
    monkeypatch.setattr("kometa.downloader.ensure_cbz", lambda p: p)
    item = next(x for x in db.get_queue(db_path) if x["id"] == qid)
    out = lp.place(item, qid, [str(dl / n) for n in names], lambda s, d: shutil.copy(s, d) and d, "Torrent", db_path, ensure_run=ensure)
    assert out["placed"] == 2 and out["skipped"] == 1
    assert os.path.exists(root / "Secret Wars" / "Secret Wars #001.cbz")
    assert os.path.exists(root / "Old Man Logan" / "Old Man Logan #002.cbz")
    assert next(x for x in db.get_queue(db_path) if x["id"] == qid)["state"] == "done"


def test_get_missing_runs_packs_first_and_searches_only_what_they_dont_fill(swlist, db_path, monkeypatch):
    monkeypatch.setattr(lg, "DB_PATH", db_path)
    res = rl.resolve(swlist, db_path)
    sw_items = [e["item_id"] for e in res["entries"] if e["series"] == "Secret Wars"]
    sid = db.add_series(title="Secret Wars", publisher="Marvel", year_began=2015, folder_path=None, on_pull_list=False, path=db_path)
    monkeypatch.setattr(lp, "_ensure_run", lambda list_id, item_id, path: db.get_series_by_id(sid, path))
    monkeypatch.setattr(lp, "find_packs", lambda list_id, path=None: {"chosen": [{"title": "SW pack", "fills": sw_items, "covers": sw_items, "protocol": "torrent", "url": "x"}]})
    queued, searched = [], []
    monkeypatch.setattr(lp, "queue_pack", lambda list_id, pack, owner, path=None: queued.append((pack["title"], owner)) or 99)
    monkeypatch.setattr(lg, "get_entry", lambda list_id, item_id, path=None, **kw: searched.append(item_id) or {"result": "queued", "queued": 1, "created": False})
    import kometa.acquisition as acq
    monkeypatch.setattr(acq, "_process_queue", lambda: None)
    out = lg.get_missing(swlist, db_path)
    assert queued == [("SW pack", sid)] and out["packs"] == 1 and out["in_packs"] == len(sw_items)
    assert not set(searched) & set(sw_items) and len(searched) == len(res["entries"]) - len(sw_items)


def test_packs_that_name_neither_the_event_nor_a_run_are_never_read(swlist, db_path):
    rows = [{"title": "X-Force (v1 - v3 + Related &amp; Extras)", "protocol": "torrent", "url": "http://prowlarr/xf", "size": 9e9, "seeders": 50}]
    fetched = []
    hunt = lp.find_packs(swlist, db_path, prowlarr=_Prowlarr(rows), fetch=lambda u: fetched.append(u) or (b"", ""))
    assert fetched == [] and hunt["candidates"] == []


def test_a_magnet_whose_swarm_never_answers_is_couldnt_look_not_zero_files(swlist, db_path):
    rows = [{"title": "Secret Wars (Story Arc) (2015-2016)", "protocol": "torrent", "url": "", "magnet": "magnet:?xt=urn:btih:abc", "size": 1e10, "seeders": 8}]
    hunt = lp.find_packs(swlist, db_path, prowlarr=_Prowlarr(rows), magnet_files=lambda m: None)
    assert hunt["candidates"] == []



def test_reading_order_prefixes_come_off_but_numeric_titles_stay(db_path):
    lid = rl.save_list("SW", [{"series": "X-Tinction Agenda", "number": "4", "volume": "2015"},
                              {"series": "1872", "number": "2", "volume": "2015"}], "cbl", None, db_path)
    gaps = lp.gap_index(lid, db_path)
    xt = gaps[lp._key("X-Tinction Agenda")]["numbers"][4.0]
    n1872 = gaps[lp._key("1872")]["numbers"][2.0]
    assert lp.match_file("Secret Wars (Story Arc) (2015-2016)/103 X-tinction Agenda 04 (of 04) (2015) (digital).cbr", gaps) == xt
    assert lp.match_file("1872 002 (2015) (Digital).cbz", gaps) == n1872
    assert lp.match_file("Fear Itself S01E03 XviD.avi", gaps) is None
