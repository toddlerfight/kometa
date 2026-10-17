"""An issue with no release date that you don't own is missing (app.js issueStatus): amber frame,
a ↓, on the Missing tab. 2026-10-17: Batgirl: Year One (2003, ended) — Metron has no store dates
for it, so all nine queued issues showed as plain covers with no frame and no way to get them."""


def test_an_undated_issue_you_dont_own_is_missing(app):
    app.wait_for_selector(".series-card")
    got = app.evaluate("""() => {
      const s = { id: 1, title: 'Batgirl: Year One', issues: [] };
      const host = document.createElement('div');
      host.innerHTML = _issueTileHtml(s, { number: 5, store_date: null, owned: 0 })
                     + _issueTileHtml(s, { number: 6, store_date: '9999-01-01', owned: 0 })
                     + _issueTileHtml(s, { number: 1, store_date: null, owned: 1 });
      const t = [...host.querySelectorAll('.issue-tile')];
      return t.map(x => ({ img: x.querySelector('.issue-tile-img').className, get: !!x.querySelector('.issue-tile-search') }));
    }""")
    assert "missing" in got[0]["img"] and got[0]["get"]               # undated, not owned: amber + ↓
    assert "upcoming" in got[1]["img"] and not got[1]["get"]          # unscheduled future: still upcoming
    assert "missing" not in got[2]["img"] and not got[2]["get"]       # owned: a plain cover
