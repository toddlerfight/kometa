"""GetComics trade post matching: the series name must LEAD into trade-speak,
not be a substring of some other title."""
from kometa.getcomics_client import _trade_post_matches, _normalize


def _m(title, post, **kw):
    return _trade_post_matches(_normalize(title), _normalize(post), post_raw=post, **kw)


def test_dienamite_is_not_die():
    assert not _m("Die", "DIE!namite Vol. 1 (TPB) (2021)", vol=1)
    assert _m("Die", "Die Vol. 1: Fantasy Heartbreaker (TPB) (2019)", vol=1)
    assert _m("Die", "Die Deluxe Edition HC (2022)")


def test_a_longer_name_sharing_a_prefix_is_not_the_run():
    assert not _m("Batman", "Batman and Robin Vol. 1 (TPB) (2012)", vol=1)
    assert _m("Batman", "Batman Vol. 1 – The Court of Owls (TPB) (2012)", vol=1)


def test_ogns_and_year_stamped_posts_still_match():
    assert _m("Gigs", "Gigs (2026)")
    assert _m("Hellboy: The Chained Coffin and Others", "Hellboy – The Chained Coffin and Others (1998)")
    assert not _m("Gigs", "Gigs #3 (2026)")


def test_loose_match_for_a_confirmed_search():
    from kometa.getcomics_client import _trade_post_matches_loose
    L = lambda t, p: _trade_post_matches_loose(_normalize(t), _normalize(p), post_raw=p)
    assert L("Hellboy: The Chained Coffin and Others", "Hellboy Vol. 3 – The Chained Coffin and Others (TPB) (2004)")
    assert L("B.P.R.D.: 1946", "B.P.R.D. – 1946 (2008)")
    assert not L("Hellboy: The Chained Coffin and Others", "Hellboy – Seed of Destruction (TPB) (1994)")
    assert not L("Hellboy: The Chained Coffin and Others", "Hellboy: The Chained Coffin and Others #2 (1998)")
