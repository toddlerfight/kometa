"""Is someone waiting on this? Set for the length of every web request (main.py
middleware); unset in scheduler jobs and the threads they start.

The rate-limited sources (LOCG's shared pace, Metron's one-at-a-time lock) let
background work queue for minutes — that's their job. A page you're looking at
must not join that queue: a cover tile falling through to LOCG held a web worker
until the pace freed up, and enough of them stalled the whole app. Interactive
callers wait at most MAX_WAIT_S, then fail fast as 'busy' and the page moves on.
"""
import contextvars

INTERACTIVE = contextvars.ContextVar("kometa_interactive", default=False)
MAX_WAIT_S = 5.0


def is_interactive() -> bool:
    return INTERACTIVE.get()


# The library harvest (docs/library-enrichment-spec.md): background trickles
# that would happily spend a whole day's Metron allowance filling the back
# catalogue. They run inside `harvesting()`; metron_client stops THEM — not
# syncs, not a page you're looking at — once the day is down to its reserve.
HARVEST = contextvars.ContextVar("kometa_harvest", default=False)


def is_harvest() -> bool:
    return HARVEST.get()


class harvesting:
    """with harvesting(): ... — mark the calls inside as harvest traffic."""
    def __enter__(self):
        self._tok = HARVEST.set(True)
        return self

    def __exit__(self, *exc):
        HARVEST.reset(self._tok)
        return False
