"""Reading-list gap covers from Metron (kometa/readlists.py metron_cover): the cover of
that issue of that run, or none. The Infinity Saga's Infinity War #1–6 all wore
Infinity Countdown: Adam Warlock #1 (2026-10-17)."""
import kometa.db as db
import kometa.readlists as rl

WARLOCK = {"id": 4652, "title": "Infinity Countdown: Adam Warlock", "year": 2018}
INFINITY_WAR = {"id": 8922, "title": "The Infinity War", "year": 1992}
MOKF = [{"id": 1, "title": "Master of Kung Fu", "year": 1974},
        {"id": 2, "title": "Master of Kung Fu", "year": 2015},
        {"id": 3, "title": "Master of Kung Fu", "year": 2017}]
ISSUES = {
    4652: [{"number": 1.0, "image": "warlock-1.jpg"}],
    8922: [{"number": float(n), "image": f"infinity-war-{n}.jpg"} for n in range(1, 7)],
    1: [{"number": float(n), "image": f"mokf-1974-{n}.jpg"} for n in range(17, 126)],
    2: [{"number": float(n), "image": f"mokf-2015-{n}.jpg"} for n in range(1, 5)],
    3: [{"number": 126.0, "image": "mokf-2017-126.jpg"}],
}


def _stubs(rows):
    asked = []

    def search(q):
        asked.append(("search", q))
        return rows.get(q, [])

    def issues(sid):
        asked.append(("issues", sid))
        return ISSUES.get(sid, [])
    return search, issues, asked


def test_a_leading_the_still_matches_and_each_issue_wears_its_own_cover():
    search, issues, asked = _stubs({"Infinity War": [WARLOCK, INFINITY_WAR]})
    memo: dict = {}
    covers = [rl.metron_cover("Infinity War", n, None, search, issues, memo) for n in ("1", "2", "6")]
    assert covers == ["infinity-war-1.jpg", "infinity-war-2.jpg", "infinity-war-6.jpg"]
    assert asked == [("search", "Infinity War"), ("issues", 8922)]          # one search, one issue list


def test_no_matching_title_or_number_means_no_cover_not_a_neighbours():
    search, issues, _ = _stubs({"Infinity War": [WARLOCK], "Some Run": [{"id": 8922, "title": "Some Run", "year": 1992}]})
    assert rl.metron_cover("Infinity War", "2", None, search, issues, {}) == ""   # the first row isn't a match
    assert rl.metron_cover("Some Run", "9", None, search, issues, {}) == ""       # #9 isn't there: not #1's cover
    assert rl.metron_cover("Some Run", None, None, search, issues, {}) == "infinity-war-1.jpg"   # a whole run wears #1


def test_the_lists_volume_year_picks_the_run():
    search, issues, _ = _stubs({"Master of Kung Fu": MOKF})
    assert rl.metron_cover("Master of Kung Fu", "1", 2015, search, issues, {}) == "mokf-2015-1.jpg"
    assert rl.metron_cover("Master of Kung Fu", "1", 2003, search, issues, {}) == ""        # no run within a year
    # no year: the first run that has the number (1974's starts at #17)
    assert rl.metron_cover("Master of Kung Fu", "2", None, search, issues, {}) == "mokf-2015-2.jpg"


def test_fill_covers_reads_the_volume_from_the_cbl(db_path):
    cbl = b"""<ReadingList><Name>Battleworld</Name><Books>
<Book Series="Master of Kung Fu" Number="1" Volume="2015" />
<Book Series="Master of Kung Fu" Number="4" Volume="2015" />
<Book Series="Infinity War" Number="3" />
</Books></ReadingList>"""
    lid = rl.import_cbl(cbl, path=db_path)
    search, issues, asked = _stubs({"Master of Kung Fu": MOKF, "Infinity War": [WARLOCK, INFINITY_WAR]})
    r = rl.fill_covers(lid, path=db_path, cv=None, metron_search=search, metron_issues=issues)
    assert r["filled"] == 3
    with db._connect(db_path) as c:
        got = [row[0] for row in c.execute("SELECT cover_url FROM reading_list_items WHERE list_id = ? ORDER BY position", (lid,))]
    assert got == ["mokf-2015-1.jpg", "mokf-2015-4.jpg", "infinity-war-3.jpg"]
    assert asked.count(("search", "Master of Kung Fu")) == 1
