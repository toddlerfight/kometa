"""Keys out of errors and logs (kometa/scrub.py). Security review 2026-10-11 item 7: a
requests error repeats the URL it tried, ?apikey= and all, into answers and docker logs."""
import logging

import kometa.main as main
from kometa.scrub import ScrubFilter, scrub


def test_keys_and_url_passwords_are_blanked():
    e = ("HTTPConnectionPool(host='sab', port=8080): Max retries exceeded with url: /api?apikey=abc123&output=json "
         "https://comicvine.gamespot.com/api/issues/?api_key=zzz&format=json http://user:" + "hunter2@gitea:3030/x.git token=t0k")
    out = scrub(e)
    for secret in ("abc123", "zzz", "hunter2", "t0k"):
        assert secret not in out
    assert "output=json" in out and "format=json" in out and "gitea:3030" in out
    assert scrub(None) is None and scrub("nothing to hide") == "nothing to hide"


def test_a_log_line_is_scrubbed_before_its_written(caplog):
    log = logging.getLogger("kometa.test_scrub")
    h = logging.Handler()
    seen = []
    h.emit = lambda r: seen.append(logging.Formatter("%(message)s").format(r))
    h.addFilter(ScrubFilter())
    log.addHandler(h)
    try:
        log.warning(f"SAB failed: GET /api?apikey=SECRETKEY&mode=queue")
        log.warning("Prowlarr %s", RuntimeError("url: /download?apikey=OTHERKEY&link=1"))
        try:
            raise ValueError("/api?api_key=TRACEKEY")
        except ValueError:
            log.exception("boom")
    finally:
        log.removeHandler(h)
    text = "\n".join(seen)
    assert "SECRETKEY" not in text and "OTHERKEY" not in text and "TRACEKEY" not in text
    assert "mode=queue" in text and "link=1" in text


def test_an_http_error_detail_is_scrubbed():
    import asyncio
    from fastapi import HTTPException
    r = asyncio.run(main._scrubbed_http_error(None, HTTPException(502, "The catalogue didn't answer: /api/issues/?api_key=CVKEY&format=json")))
    assert r.status_code == 502 and b"CVKEY" not in r.body and b"format=json" in r.body
