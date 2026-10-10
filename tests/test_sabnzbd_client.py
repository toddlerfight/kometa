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


def test_delete_job_clears_queue_and_history():
    c = _Fake()
    assert c.delete_job("SABnzbd_nzo_1")
    assert [x["mode"] for x in c.calls] == ["queue", "history"] and all(x["del_files"] == 1 for x in c.calls)
