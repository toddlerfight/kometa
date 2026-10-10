"""Reading wins. Background sweeps — file covers, the backup mirror, image
fetches, the LOCG sweep — share the NAS and the CPU with the reader, and on
2026-10-10 the cover sweep plus the first backup mirror pushed page loads to
three seconds on both the web and YondeB. Every page served stamps the clock;
every background loop asks `yield_to_reader()` between items and sleeps while
someone has turned a page in the last READING_WINDOW_S seconds.
"""
import threading
import time

READING_WINDOW_S = 90
_last_page = {"at": 0.0}
_lock = threading.Lock()


def note_reading() -> None:
    with _lock:
        _last_page["at"] = time.time()


def reading_recently(window: float = READING_WINDOW_S) -> bool:
    return time.time() - _last_page["at"] < window


def yield_to_reader(sleep=time.sleep, max_wait: float = 600.0) -> bool:
    """Block (in small sleeps) while a reader is active; True if we waited."""
    waited = False
    start = time.time()
    while reading_recently() and time.time() - start < max_wait:
        sleep(5)
        waited = True
    return waited
