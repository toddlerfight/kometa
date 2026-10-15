"""The series page's run badge (kometa/runstatus.py) and the harvest tick that
asks Metron for the status (kometa/harvest.py status_tick). No network."""
import kometa.db as db
from kometa import harvest, runstatus

TODAY = "2026-10-13"


def _iss(*dates):
    return [{"number": float(n), "store_date": d} for n, d in enumerate(dates, 1)]


def test_something_solicited_is_ongoing_whatever_metron_says():
    s = {"metron_status": "Completed", "metron_type": "Limited Series"}
    assert runstatus.compute(s, _iss("2026-09-01", "2026-11-18"), TODAY)["state"] == "ongoing"


def test_metron_completed_and_cancelled_are_finished():
    assert runstatus.compute({"metron_status": "Completed"}, _iss("2020-01-01"), TODAY)["label"] == "Ended"
    assert runstatus.compute({"metron_status": "Cancelled"}, _iss("2020-01-01"), TODAY)["label"] == "Ended"


def test_metron_hiatus_is_hiatus():
    assert runstatus.compute({"metron_status": "Hiatus"}, _iss("2026-09-01"), TODAY)["state"] == "hiatus"


def test_a_limited_series_with_nothing_to_come_is_finished():
    s = {"metron_status": "Ongoing", "metron_type": "Limited Series"}
    assert runstatus.compute(s, _iss("2026-09-01"), TODAY)["state"] == "finished"


def test_a_year_ended_is_finished():
    assert runstatus.compute({"metron_status": "Ongoing", "metron_year_end": 2024}, _iss("2024-06-01"), TODAY)["state"] == "finished"


def test_metrons_default_ongoing_never_keeps_a_dead_run_alive():
    s = {"metron_status": "Ongoing", "metron_type": "Single Issue"}
    assert runstatus.compute(s, _iss("2026-08-20"), TODAY)["state"] == "ongoing"      # between solicits
    assert runstatus.compute(s, _iss("2025-12-01"), TODAY)["state"] == "hiatus"
    assert runstatus.compute(s, _iss("2019-03-01"), TODAY)["state"] == "finished"


def test_no_dates_and_no_metron_word_is_no_badge():
    assert runstatus.compute({}, [{"number": 1.0, "store_date": None}], TODAY) is None


class FakeMetron:
    class MetronUnavailable(RuntimeError):
        pass

    def __init__(self, spent_after=99):
        self.asked, self.spent_after = [], spent_after

    def configured(self):
        return True

    def harvest_share_spent(self):
        return len(self.asked) >= self.spent_after

    def series_detail(self, mid):
        self.asked.append(mid)
        return {"type": "Single Issue", "status": "Completed", "year_end": 2024}


def test_status_tick_fills_status_and_never_asks_a_finished_run_again(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = 77 WHERE id = ?", (sid,))
    m = FakeMetron()
    assert harvest.status_tick(path=db_path, client=m) == 1
    s = db.get_series_by_id(sid, db_path)
    assert (s["metron_status"], s["metron_year_end"], s["metron_type"]) == ("Completed", 2024, "Single Issue")
    assert harvest.status_tick(path=db_path, client=m) == 0          # Completed: done for good
    assert m.asked == [77]


def test_status_tick_stops_at_the_harvest_floor(db_path):
    for t in ("A", "B", "C"):
        sid = db.add_series(title=t, publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
        with db._connect(db_path) as conn:
            conn.execute("UPDATE tracked_series SET metron_series_id = ? WHERE id = ?", (sid + 100, sid))
    m = FakeMetron(spent_after=2)
    assert harvest.status_tick(path=db_path, client=m) == 2
