"""Fandom cover galleries (fandom_client.gallery / parse_gallery). Offline."""
import kometa.fandom_client as fc

TITLE = "Midnight Spider-Man Vol 1 1"
ANSWER = {"query": {"pages": {
    "-1": {"title": "File:Marvel Unlimited logo.png", "imageinfo": [{"url": "https://w/logo.png", "width": 214, "height": 256, "mime": "image/png"}]},
    "-2": {"title": f"File:{TITLE} 616 Comics Exclusive Variant.jpg",
           "imageinfo": [{"url": "https://w/616.jpg/revision/latest?cb=1", "width": 1332, "height": 2048, "mime": "image/jpeg"}]},
    "-3": {"title": f"File:{TITLE} Artgerm.com Exclusive Virgin Variant.jpg",
           "imageinfo": [{"url": "https://w/artgerm.jpg", "width": 950, "height": 1463, "mime": "image/jpeg"}]},
    "-4": {"title": f"File:{TITLE} Textless.jpg", "imageinfo": [{"url": "https://w/tiny.jpg", "width": 120, "height": 180, "mime": "image/jpeg"}]},
    "-5": {"title": "File:Some Other Book Vol 1 1.jpg", "imageinfo": [{"url": "https://w/other.jpg", "width": 1000, "height": 1500, "mime": "image/jpeg"}]},
}}}


def test_page_title_follows_the_wikis():
    assert fc.page_title("Midnight Spider-Man", 1, 1.0) == TITLE
    assert fc.page_title("Fear Itself - The Worthy", None, 3) == "Fear Itself: The Worthy Vol 1 3"
    assert fc.page_title("Batman (2016)", 3, 50.0) == "Batman Vol 3 50"
    assert fc.page_title("Fantastic Four", 1, 605.1) == "Fantastic Four Vol 1 605.1"


def test_only_the_issues_own_covers_big_enough():
    g = fc.parse_gallery(ANSWER, TITLE)
    assert [c["name"] for c in g] == ["616 Comics Exclusive Variant", "Artgerm.com Exclusive Virgin Variant"]
    assert g[0]["width"] == 1332


def test_missing_page_is_none_and_redirects_are_followed():
    assert fc.parse_gallery({"query": {"pages": {"-1": {"title": TITLE, "missing": ""}}}}, TITLE) is None
    moved = {"query": {"redirects": [{"from": "Old Name Vol 1 1", "to": TITLE}], "pages": ANSWER["query"]["pages"]}}
    assert len(fc.parse_gallery(moved, "Old Name Vol 1 1")) == 2


def test_the_shared_throttle_stops_at_the_daily_cap(monkeypatch):
    import pytest
    monkeypatch.setattr(fc, "_last", {"t": 0.0, "day": __import__("time").strftime("%Y-%m-%d"), "count": fc.DAILY_CAP, "paused_until": 0.0})
    with pytest.raises(fc.FandomUnavailable, match="spent"):
        fc.gallery("marvel", TITLE)
