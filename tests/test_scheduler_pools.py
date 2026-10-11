"""The 5-second pollers and the queue run on their own pool, and the scheduler
stops when asked (kometa/scheduler.py, main.lifespan)."""
from kometa.scheduler import start_scheduler


def test_pollers_have_their_own_pool_and_the_scheduler_stops():
    noop = lambda *a, **k: None
    s = start_scheduler(noop, noop, noop, noop, noop)
    try:
        jobs = {j.id: j for j in s.get_jobs()}
        assert {jobs[i].executor for i in ("queue_processor", "usenet_poller", "torrent_poller")} == {"fast"}
        assert jobs["usenet_poller"].coalesce
    finally:
        s.shutdown(wait=False)
    assert not s.running
