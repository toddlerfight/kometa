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
# set when the app is shutting down: a loop waiting on a reader gives up at once
# instead of holding the process open (a deploy waited out the 10-minute yields)
STOPPING = threading.Event()


_book_pages: dict[int, float] = {}       # book id → when a page of it was last served


def note_reading(book_id: int | None = None) -> None:
    with _lock:
        now = time.time()
        _last_page["at"] = now
        if book_id is not None:
            _book_pages[int(book_id)] = now


def last_page_served(book_id: int) -> float:
    """When a page of this book was last served (0 = not since start-up). One of
    the two 'is this book open' signals kometa/localfirst.py reads."""
    with _lock:
        return _book_pages.get(int(book_id), 0.0)


def reading_recently(window: float = READING_WINDOW_S) -> bool:
    return time.time() - _last_page["at"] < window


def yield_to_reader(sleep=time.sleep, max_wait: float = 600.0) -> bool:
    """Block (in small sleeps) while a reader is active; True if we waited."""
    waited = False
    start = time.time()
    while reading_recently() and time.time() - start < max_wait and not STOPPING.is_set():
        if sleep is time.sleep:
            STOPPING.wait(5)
        else:
            sleep(5)
        waited = True
    return waited
