"""'Search now' during a running worker pass is remembered, not dropped."""
import threading

import kometa.acquisition as acq


def test_a_request_during_a_pass_makes_the_pass_go_again(monkeypatch):
    runs = []
    first_started = threading.Event()
    release = threading.Event()

    def fake_pass():
        runs.append(1)
        if len(runs) == 1:
            first_started.set()
            release.wait(timeout=5)                      # hold the lock like a long download
    monkeypatch.setattr(acq, "_process_queue_locked", fake_pass)
    monkeypatch.setattr(acq, "_rerun_wanted", False)

    t = threading.Thread(target=acq._process_queue)
    t.start()
    assert first_started.wait(timeout=5)
    assert acq.queue_busy() is True
    acq._process_queue()                                 # 'Search now' while busy: returns at once…
    assert len(runs) == 1
    release.set()
    t.join(timeout=5)
    assert len(runs) == 2                                # …and the pass went again when free
    assert acq.queue_busy() is False
