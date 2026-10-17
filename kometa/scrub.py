"""Keys out of text that leaves the process: HTTP errors, test results and logs.

requests repeats the URL it tried, query string and all, and SAB's and ComicVine's
keys ride there (security review 2026-10-11, item 7). Queue errors had their own
blanking already (db.plain_error); this is the same idea for everything else.
"""
import logging
import re

_KV = re.compile(r"(?i)\b(api_?key|api-key|x-api-key|token|access_token|password|passwd|cf_clearance)(\s*[=:]\s*)(?![\s\"'])([^&\s\"'<>,;)]+)")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")


def scrub(text):
    """The text with every key=value secret and URL password blanked; anything else as given."""
    if not text or not isinstance(text, str):
        return text
    return _USERINFO.sub(r"\1***@", _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text))


class ScrubFilter(logging.Filter):
    """On a handler: the message, its arguments and any traceback, scrubbed before they're written.
    Arguments stay a tuple (uvicorn's access formatter reads them by position)."""

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(str(a)) if isinstance(a, (str, BaseException)) else a for a in record.args)
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return True


def install(*names: str) -> None:
    """Every handler on the root logger and the named ones gets the filter, once."""
    for lg in (logging.getLogger(), *(logging.getLogger(n) for n in names)):
        for h in lg.handlers:
            if not any(isinstance(f, ScrubFilter) for f in h.filters):
                h.addFilter(ScrubFilter())
