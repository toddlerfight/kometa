"""One LOCG pace for every caller (kometa/locg_client._pace)."""
import kometa.locg_client as lc


def test_pace_spaces_requests_and_caps_the_window():
    clock = [1000.0]
    slept = []
    def sleep(s): slept.append(s); clock[0] += s
    now = lambda: clock[0]
    lc._recent.clear()
    for _ in range(lc.PACE_MAX):
        lc._pace(sleep, now)
    assert all(s >= lc.PACE_GAP_S for s in slept)          # never two inside the gap
    before = clock[0]
    lc._pace(sleep, now)                                    # the 21st waits out the window
    assert clock[0] - lc._recent[0] < lc.PACE_WINDOW_S and clock[0] - before >= 1
    assert len([x for x in lc._recent if clock[0] - x < lc.PACE_WINDOW_S]) <= lc.PACE_MAX
    lc._recent.clear()


def test_a_429_keeps_the_pass(monkeypatch):
    forgot = []
    monkeypatch.setattr(lc, "_access", lambda: ("cookie", "ua"))
    monkeypatch.setattr(lc, "_forget_access", lambda: forgot.append(1))
    monkeypatch.setattr(lc, "_pause", {"until": 0})
    class R: status_code = 429; headers = {}
    try:
        lc._note_refusal(R())
    except Exception:
        pass
    assert forgot == []
