"""The usenet release scorer (kometa/usenet_client._nzb_score): the series name must LEAD."""
from kometa.usenet_client import _nzb_score


def test_a_name_found_mid_title_is_a_different_comic():
    assert _nzb_score("1001.Arabian.Nights-The.Adventures.Of.Sinbad.012", "Nights", 12) == 0
    assert _nzb_score("Nights.012.2025.Digital.Empire", "Nights", 12) > 0
    assert _nzb_score("Nights 012 (2025) (Digital)", "Nights", 12) > 0
    assert _nzb_score("[Group] Nights 012 (2025)", "Nights", 12) > 0            # a leading group tag is fine
    assert _nzb_score("Batman - Nights of the Owl 012", "Nights", 12) == 0
