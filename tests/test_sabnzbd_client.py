"""SABnzbd client: a comic never waits behind the movie stack's 4K remuxes."""
from kometa.sabnzbd_client import SABnzbdClient


class _Fake(SABnzbdClient):
    def __init__(self, **kw):
        super().__init__("http://sab", "k", **kw)
        self.calls = []

    def _api(self, **params):
        self.calls.append(params)
        if params["mode"] == "addurl":
            return {"nzo_ids": ["SABnzbd_nzo_1"]}
        return {"status": True}


def test_a_comic_goes_in_high_and_to_the_front():
    c = _Fake()
    assert c.add_nzb_url("http://prowlarr/x.nzb", "DIE Loaded 1") == "SABnzbd_nzo_1"
    assert c.calls[0]["mode"] == "addurl" and c.calls[0]["priority"] == 1
    assert c.calls[1] == {"mode": "switch", "value": "SABnzbd_nzo_1", "value2": 0}


def test_priority_and_jumping_are_config():
    c = _Fake(priority=0, jump_queue=False)
    c.add_nzb_url("http://prowlarr/x.nzb")
    assert c.calls[0]["priority"] == 0 and len(c.calls) == 1


def test_force_is_clamped_to_high():
    c = _Fake(priority=2)
    c.add_nzb_url("http://prowlarr/x.nzb")
    assert c.calls[0]["priority"] == 1                      # Force would walk through a paused queue


def test_poll_job_carries_the_queue_position():
    c = _Fake()
    snap = {"queue": {"n1": {"nzo_id": "n1", "status": "Queued", "index": 3, "percentage": "0", "mbleft": "40"}},
            "history": {}}
    r = c.poll_job("n1", snap)
    assert r["index"] == 3 and r["sab_status"] == "Queued"


def test_delete_job_clears_queue_and_history():
    c = _Fake()
    assert c.delete_job("SABnzbd_nzo_1")
    assert [x["mode"] for x in c.calls] == ["queue", "history"] and all(x["del_files"] == 1 for x in c.calls)


def test_completed_with_missing_articles_and_no_repair_is_failed():
    """Puzzlebox #4, 2026-10-16: SAB 'Completed', stage log '1 articles were missing',
    Repair stage only 'Trying RAR renamer' — the pages came out truncated."""
    from kometa.sabnzbd_client import incomplete_reason
    c = _Fake()
    slot = {"nzo_id": "n9", "status": "Completed", "storage": "/dl/x", "stage_log": [
        {"name": "Download", "actions": ["Downloaded in 3 seconds at an average of 11.2 MB/s<br/>Age: 1912d<br/>1 articles were missing"]},
        {"name": "Repair", "actions": ["Trying RAR renamer"]}]}
    assert incomplete_reason(slot) == "incomplete: 1 article missing and no repair"
    r = c.poll_job("n9", {"queue": {}, "history": {"n9": slot}})
    assert r["status"] == "failed" and "missing" in r["error"]
    slot["stage_log"][1] = {"name": "Repair", "actions": ["[name.par2] Verified in 2 seconds, repair is required", "Repaired in 4 seconds"]}
    assert incomplete_reason(slot) is None
    assert c.poll_job("n9", {"queue": {}, "history": {"n9": slot}})["status"] == "completed"
    whole = {"nzo_id": "n8", "status": "Completed", "storage": "/dl/y", "stage_log": [{"name": "Download", "actions": ["Downloaded in 5 seconds"]}]}
    assert c.poll_job("n8", {"queue": {}, "history": {"n8": whole}})["status"] == "completed"
