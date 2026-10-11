"""One SAB call pair and one qBit call per poll tick, read per job from the snapshot."""
from kometa.sabnzbd_client import SABnzbdClient
from kometa.qbittorrent_client import QBittorrentClient


def test_sab_jobs_read_from_one_snapshot():
    sab = SABnzbdClient.__new__(SABnzbdClient)
    calls = []
    def api(**p):
        calls.append(p["mode"])
        if p["mode"] == "queue":
            return {"queue": {"slots": [{"nzo_id": "a", "percentage": "40", "mbleft": "10", "status": "Downloading"}]}}
        return {"history": {"slots": [{"nzo_id": "b", "status": "Completed", "storage": "/d/b"},
                                      {"nzo_id": "c", "status": "Failed", "fail_message": "repair"}]}}
    sab._api = api
    snap = sab.snapshot(["a", "b", "c", "d"])
    got = {n: sab.poll_job(n, snap)["status"] for n in "abcd"}
    assert got == {"a": "queued", "b": "completed", "c": "failed", "d": "unknown"}
    assert calls == ["queue", "history"]


def test_qbit_torrents_read_from_one_snapshot():
    q = QBittorrentClient.__new__(QBittorrentClient)
    calls = []
    class R:
        def json(self): return [{"hash": "ABC", "state": "uploading", "content_path": "/d/abc"},
                                {"hash": "def", "state": "downloading", "progress": 0.5, "num_complete": 3}]
    q._req = lambda *a, **k: (calls.append(k["params"]["hashes"]), R())[1]
    snap = q.snapshot(["abc", "def", "zzz"])
    assert q.poll_job("abc", snap)["status"] == "completed"
    assert q.poll_job("def", snap)["pct"] == 50.0
    assert q.poll_job("zzz", snap)["status"] == "unknown"
    assert calls == ["abc|def|zzz"]
