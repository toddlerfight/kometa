"""A request someone is waiting on never queues behind background work on the
rate-limited sources (kometa/interactive.py)."""
import threading
import time

import pytest

import kometa.locg_client as lc
import kometa.metron_client as mc
from kometa.interactive import INTERACTIVE


def test_locg_pace_fails_fast_for_a_page_but_waits_for_background(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(lc, "_recent", [now - i for i in range(lc.PACE_MAX)])      # the window is full
    with pytest.raises(lc.LocgBusy):
        lc._pace(sleep=lambda s: pytest.fail("a page slept on LOCG's pace"), now=lambda: now, max_wait=5)
    slept = []
    lc._pace(sleep=slept.append, now=lambda: now)                                    # background: waits its turn
    assert slept and slept[0] > 5


def test_metron_lock_held_by_background_makes_a_page_fail_fast(monkeypatch):
    monkeypatch.setattr(mc, "_creds", lambda: ("token", "t"))
    monkeypatch.setattr(mc, "_refused", lambda c: False)
    monkeypatch.setattr(mc, "MAX_WAIT", None, raising=False)
    import kometa.interactive as it
    monkeypatch.setattr(it, "MAX_WAIT_S", 0.2)
    mc._lock.acquire()                                                               # a background job mid-request
    try:
        tok = INTERACTIVE.set(True)
        t = time.time()
        with pytest.raises(mc.MetronUnavailable, match="busy"):
            mc._get("series/")
        assert time.time() - t < 2
        INTERACTIVE.reset(tok)
    finally:
        mc._lock.release()
