"""The sourcing matcher reads the files (kometa/sourcing.py check_with_files / verify_deck):
each candidate run is asked for the files' own issue; another year's issue drops the run,
and the one run that agrees on year AND writer is linked with no card. 2026-10-17: the deck
offered the 1968 Captain Marvel beside the 2019 run the files are (Kelly Thompson, 2019)."""
import json
import zipfile

import kometa.db as db
import kometa.metron_client as mc
import kometa.sourcing as src

CM_1968 = {"id": 1, "name": "Captain Marvel", "publisher": "Marvel", "year": 1968, "issue_count": 62}
CM_2019 = {"id": 2, "name": "Captain Marvel", "publisher": "Marvel", "year": 2019, "issue_count": 50}
CM_2019B = {"id": 3, "name": "Captain Marvel", "publisher": "Marvel", "year": 2019, "issue_count": 5}


class Metron:
    def __init__(self, runs, issues, credits, down=False):
        self.runs, self.issues, self.credits, self.down = runs, issues, credits, down
        self.asked = []

    def configured(self):
        return True

    def search_series(self, q):
        self.asked.append(("search", q))
        return self.runs

    def _get(self, path, **kw):
        if self.down:
            raise mc.MetronUnavailable("resting")
        self.asked.append(("issue", kw["series_id"], kw["number"]))
        hit = self.issues.get((kw["series_id"], kw["number"]))
        return {"results": [hit] if hit else []}

    def issue_detail(self, iid):
        self.asked.append(("detail", iid))
        return {"credits": [{"role": "Writer", "name": n} for n in self.credits.get(iid, [])]}


def _series(db_path, tmp_path, title="Captain Marvel", files=((1, 2019, "Kelly Thompson"), (2, 2019, "Kelly Thompson"))):
    sid = db.add_series(title=title, publisher="Marvel Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET locg_series_id = 555 WHERE id = ?", (sid,))
        for n, year, writer in files:
            p = tmp_path / f"{title} #{n:03d}.cbz"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("ComicInfo.xml", f"<ComicInfo><Series>{title}</Series><Number>{n}</Number>"
                                            f"<Year>{year}</Year><Writer>{writer}</Writer></ComicInfo>")
                z.writestr("001.jpg", b"x")
            c.execute("INSERT INTO books (path, tracked_series_id, number) VALUES (?, ?, ?)", (str(p), sid, float(n)))
            c.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, ?, 1)", (sid, float(n)))
    return sid


def _row(db_path, sid):
    with db._connect(db_path) as c:
        r = c.execute("SELECT metron_series_id, metron_candidates_json FROM tracked_series WHERE id = ?", (sid,)).fetchone()
    return r[0], json.loads(r[1] or "[]")


ISSUES = {(1, "1"): {"id": 901, "cover_date": "1968-05-01"}, (2, "1"): {"id": 902, "cover_date": "2019-03-01"},
          (3, "1"): {"id": 903, "cover_date": "2019-06-01"}}


def test_the_run_the_files_agree_with_is_linked_and_another_years_run_is_dropped(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    m = Metron([CM_1968, CM_2019], ISSUES, {902: ["Kelly Thompson"]})
    assert src.sourcing_tick(path=db_path, client=m) == 1
    linked, cands = _row(db_path, sid)
    assert linked == 2
    assert [(c["id"], c["files"]) for c in cands] == [(2, "agree")]                 # 1968 is gone from the card
    assert ("detail", 901) not in m.asked                                           # another year: no credits asked


def test_a_writer_that_doesnt_match_leaves_the_card_for_you(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    m = Metron([CM_2019], ISSUES, {902: ["Someone Else"]})
    src.sourcing_tick(path=db_path, client=m)
    linked, cands = _row(db_path, sid)
    assert linked is None and [(c["id"], c["files"]) for c in cands] == [(2, "year")]


def test_two_runs_that_both_agree_are_left_for_you(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    m = Metron([CM_2019, CM_2019B], ISSUES, {902: ["Kelly Thompson"], 903: ["Kelly Thompson"]})
    src.sourcing_tick(path=db_path, client=m)
    linked, cands = _row(db_path, sid)
    assert linked is None and sorted(c["id"] for c in cands) == [2, 3]


def test_no_files_no_issue_calls(db_path, tmp_path):
    sid = _series(db_path, tmp_path, files=())
    m = Metron([CM_2019], ISSUES, {})
    src.sourcing_tick(path=db_path, client=m)
    linked, cands = _row(db_path, sid)
    assert linked is None and cands[0]["files"] == "none"
    assert all(a[0] == "search" for a in m.asked)


def test_metron_resting_at_the_file_check_stores_nothing(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    m = Metron([CM_2019], ISSUES, {902: ["Kelly Thompson"]}, down=True)
    assert src.sourcing_tick(path=db_path, client=m) == 0
    assert _row(db_path, sid) == (None, [])                      # searched again next tick


def test_cards_already_in_the_deck_are_checked_once(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    old = [dict(CM_1968, title="Captain Marvel", score=85.0, confidence="high"),
           dict(CM_2019, title="Captain Marvel", score=85.0, confidence="high")]
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_candidates_json = ? WHERE id = ?", (json.dumps(old), sid))
    m = Metron([], ISSUES, {902: ["Kelly Thompson"]})
    src.sourcing_tick(path=db_path, client=m)
    assert _row(db_path, sid)[0] == 2
    m.asked.clear()
    src.sourcing_tick(path=db_path, client=m)
    assert m.asked == []                                          # linked: off the deck, never asked again


def test_a_declined_series_is_never_rechecked(db_path, tmp_path):
    sid = _series(db_path, tmp_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_candidates_json = ?, locg_link = 'none' WHERE id = ?",
                  (json.dumps([dict(CM_2019, title="Captain Marvel", score=85.0, confidence="high")]), sid))
    m = Metron([], ISSUES, {902: ["Kelly Thompson"]})
    src.verify_deck(db_path, m)
    assert m.asked == [] and _row(db_path, sid)[0] is None
