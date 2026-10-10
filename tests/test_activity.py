"""Reading wins (kometa/activity.py): background loops wait while pages are being served."""
import kometa.activity as act


def test_background_yields_while_a_reader_is_active(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(act.time, "time", lambda: now[0])
    act._last_page["at"] = 0.0
    assert not act.reading_recently()
    act.note_reading()
    assert act.reading_recently()
    slept = []
    def sleep(s): slept.append(s); now[0] += 60          # a minute passes per nap
    assert act.yield_to_reader(sleep=sleep) is True
    assert len(slept) == 2 and not act.reading_recently()   # two naps and the 90 s window has passed
    assert act.yield_to_reader(sleep=sleep) is False
