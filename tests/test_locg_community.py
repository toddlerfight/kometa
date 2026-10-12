"""LOCG community stats parser (locg_client.parse_community). A hand-made page
fragment in LOCG's markup shape (2026-10-12) — not a copy of their page."""
from kometa.locg_client import parse_community

PAGE = """<html><body>
<div class="comic-score"><div class="stat"><span class="rating-avg color-primary">4.2</span></div>
  <div class="text"><div class="font-weight-bold color1">Very Positive</div>
  <div class="text-capitalize"><strong>5,888</strong> Ratings</div></div></div>
<div class="d-flex">
  <div class="copy-medium"><i class="cg-icon-pull color1"></i><span class="ml-1">23,355</span></div>
  <div class="copy-medium"><i class="cg-icon-collect color2"></i><span class="ml-1">9,399</span></div>
  <div class="copy-medium"><i class="cg-icon-readlist color3"></i><span class="ml-1">8,751</span></div>
  <div class="copy-medium"><i class="cg-icon-wishlist color4"></i><span class="ml-1">2,366</span></div>
</div>
<a href="#reviews">1 Reviews</a>
<div class="col copy-small font-italic">Comic · 40 pages · $5.99</div>
<script>var data = google.visualization.arrayToDataTable([
  ['Rating', 'Users'],
  ['1', 18], ['2', 13], ['10', 1402]        ]);</script>
</body></html>"""


def test_everything_the_page_prints():
    c = parse_community(PAGE)
    assert c == {"rating": 4.2, "ratings": 5888, "verdict": "Very Positive", "pulls": 23355, "collected": 9399,
                 "read": 8751, "wanted": 2366, "reviews": 1, "histogram": {1: 18, 2: 13, 10: 1402},
                 "pages": 40, "price": "5.99"}


def test_a_page_without_stats_is_empty_not_an_error():
    assert parse_community("<html><body><p>nothing here</p></body></html>") == {}
