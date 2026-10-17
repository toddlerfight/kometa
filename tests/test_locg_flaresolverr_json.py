"""JSON that comes back through FlareSolverr is unescaped before use (locg_client._clean_json_text).
2026-10-17: Chrome renders JSON as text in <pre>, so LOCG's list HTML arrived as '&lt;li&gt;';
the parser found no rows and every series and trade list read through FlareSolverr was empty."""
import json

from kometa import locg_client as lc

ROW = ('<li class="issue"><div class="title"><a href="/comic/111/hellblazer-1">Hellblazer #1</a></div>'
       '<span class="date" data-date="567993600"></span><img data-src="https://s3.amazonaws.com/comicgeeks/comics/covers/medium-111.jpg"></li>')


def _as_chrome_shows_it(obj) -> str:
    import html
    return ('<html><head><meta name="color-scheme" content="light dark"></head><body><pre>'
            + html.escape(json.dumps(obj), quote=False) + '</pre></body></html>')


def test_a_list_through_flaresolverr_parses_to_its_rows():
    page = _as_chrome_shows_it({"series": {"title": "Hellblazer"}, "count": 1, "list": f"<ul>{ROW}</ul>"})
    r = lc.FlareSolverrResponse(200, page, url="https://leagueofcomicgeeks.com/comic/get_comics")
    assert "<li" in r.json()["list"]
    rows = lc._get_issues_with_get(100117, lambda url, **kw: r)
    assert rows == [{"number": 1.0, "store_date": "1988-01-01", "cover": "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-111.jpg",
                     "locg_issue_id": "111"}]


def test_plain_json_and_ampersands_in_text_survive():
    page = _as_chrome_shows_it({"title": "Tom & Jerry", "list": ""})
    assert lc.FlareSolverrResponse(200, page).json()["title"] == "Tom & Jerry"
    assert lc._clean_json_text('{"a": 1}') == '{"a": 1}'
