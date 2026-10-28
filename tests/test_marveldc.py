"""What's left of the Marvel.com scraper (retired 2026-10-15, core removed 2026-10-29):
the night-window helpers and nothing on the Settings or harvest surfaces."""
import kometa.record as rec
from kometa import harvest, marveldc


def test_window_wraps_midnight():
    assert marveldc.parse_hours("23-7") == (23, 7) and marveldc.parse_hours("junk") == (0, 0)
    assert marveldc.valid_hours("23-7") and not marveldc.valid_hours("25-7") and not marveldc.valid_hours("")
    assert [h for h in range(24) if marveldc.in_window(h, 23, 7)] == [0, 1, 2, 3, 4, 5, 6, 23]
    assert [h for h in range(24) if marveldc.in_window(h, 1, 4)] == [1, 2, 3]
    assert all(marveldc.in_window(h, 0, 0) for h in range(24))


def test_harvest_progress_has_no_marvel_row(db_path):
    rec.ensure_tables(db_path)
    p = harvest.progress(db_path)
    assert "marveldc" not in p
