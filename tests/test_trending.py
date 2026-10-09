"""Trending (kometa/trending.py): ICv2's charts parsed, matched to the shelf, enriched from Metron."""
import kometa.db as db
import kometa.trending as tr

PAGE = """<table><tr><td>Top 50 Comic Books by Units – September 2026</td></tr>
<tr><th>Rank</th><th>Title</th><th>Publisher</th><th>Price</th></tr>
<tr><td>1</td><td>Batman Day 2026 - Batman of Two Worlds</td><td>DC Comics</td><td>$2.99</td></tr>
<tr><td>2</td><td>Absolute Batman #24</td><td>DC Comics</td><td>$4.99</td></tr>
<tr><td>6</td><td>Absolute Cassandra Cain: The Shadows Hand #! (One-Shot)</td><td>DC Comics</td><td>$4.99</td></tr>
</table><table><tr><td>by dollars</td></tr></table>"""
GN = """<table><tr><th>Rank</th><th>Title</th><th>Publisher</th><th>Price</th></tr>
<tr><td>1</td><td>Absolute Batman Vol. 3 Devil's Workshop</td><td>DC Comics</td><td>$19.99</td></tr></table>"""
INDEX = '<a href="https://icv2.com/articles/markets/view/63554/top-50-comics-september-2026">Top 50 Comics - September 2026</a><a href="https://icv2.com/articles/markets/view/63565/top-20-graphic-novels-september-2026">Top 20 Graphic Novels - September 2026</a>'


def test_titles_parse_into_series_number_or_volume():
    assert tr.parse_title("Absolute Batman #24") == {"series": "Absolute Batman", "number": 24.0, "vol": None}
    assert tr.parse_title("Absolute Cassandra Cain: The Shadows Hand #! (One-Shot)")["number"] == 1.0
    v = tr.parse_title("Absolute Batman Vol. 3 Devil's Workshop")
    assert (v["series"], v["vol"], v["subtitle"]) == ("Absolute Batman", 3, "Devil's Workshop")
    assert tr.parse_title("Batman Day 2026 - Batman of Two Worlds")["number"] is None


def test_fetch_parses_both_charts_by_units_only(db_path):
    pages = {tr.INDEX_URL: INDEX, "https://icv2.com/articles/markets/view/63554/top-50-comics-september-2026": PAGE,
             "https://icv2.com/articles/markets/view/63565/top-20-graphic-novels-september-2026": GN}
    d = tr.fetch_icv2(http=lambda u: pages[u])
    assert d["month"] == "September 2026" and [e["rank"] for e in d["comics"]] == [1, 2, 6]
    assert d["graphic_novels"][0]["vol"] == 3


def test_shelf_match_and_metron_enrichment(db_path):
    sid = db.add_series(title="Absolute Batman", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 24.0, "2026-09-03", 1, path=db_path)
    data = {"comics": tr.parse_chart(PAGE), "graphic_novels": []}
    tr.match_shelf(data["comics"], db_path)
    by = {e["rank"]: e for e in data["comics"]}
    assert by[2]["owned"] and by[2]["have_issue"] and by[2]["cover"].endswith("/issues/24/thumbnail")
    assert not by[1]["owned"]
    asked = []
    def lookup(series, number):
        asked.append((series, number))
        return {"results": [{"series": {"id": 500, "name": "Batman Day 2026 (2026)", "year_began": 2026}, "image": "https://m/x.jpg"}]} if "Batman Day" in series else {"results": []}
    n = tr.enrich_catalogue(data, db_path, lookup=lookup)
    assert n == 2 and by[1]["metron_series_id"] == 500 and by[1]["cover"] == "https://m/x.jpg" and by[6]["catalogue_miss"]
    assert ("Absolute Batman", 24.0) not in asked          # owned rows aren't looked up
