"""After the wall, the scheduled LOCG sweep rests — 1 h, then 2, 4 … 12 — and starts over at
the first page that comes back (locg_sweep.scheduled_tick). 2026-10-17: it met a check
FlareSolverr can't pass every 10 minutes all afternoon."""
import kometa.locg_sweep as sw

WALL = {"requests": 3, "variants": 0, "details": 0, "community": 0, "stopped": "refused: 3 pages in a row — the wall (HTTP 500)"}
OK = {"requests": 4, "variants": 2, "details": 1, "community": 0, "stopped": None}


def test_the_sweep_rests_longer_each_time_and_starts_over_after_a_success():
    sw._rest.update(until=0.0, strikes=0)
    t = {"now": 1_000_000.0}
    ran = []

    def tick(result):
        return lambda: ran.append(result) or result
    clock = lambda: t["now"]
    assert sw.scheduled_tick(clock, tick(WALL))["stopped"].startswith("refused")
    assert sw.scheduled_tick(clock, tick(OK))["stopped"].startswith("resting")      # not asked at all
    assert len(ran) == 1
    t["now"] += 3600 + 1                                                           # an hour on: tries again
    sw.scheduled_tick(clock, tick(WALL))
    assert sw._rest["until"] - t["now"] == 2 * 3600                                # the second wall: two hours
    for _ in range(6):
        t["now"] = sw._rest["until"] + 1
        sw.scheduled_tick(clock, tick(WALL))
    assert sw._rest["until"] - t["now"] == sw.REST_MAX_S                           # never more than 12 h
    t["now"] = sw._rest["until"] + 1
    sw.scheduled_tick(clock, tick(OK))
    assert sw._rest["strikes"] == 0
    t["now"] += 1
    assert sw.scheduled_tick(clock, tick(OK))["stopped"] is None                   # back to every tick
    sw._rest.update(until=0.0, strikes=0)


def test_a_tick_stopped_for_another_reason_doesnt_rest():
    sw._rest.update(until=0.0, strikes=0)
    sw.scheduled_tick(lambda: 5.0, lambda: {"requests": 0, "variants": 0, "details": 0, "stopped": "sync in progress"})
    assert sw._rest == {"until": 0.0, "strikes": 0}
