"""Buffy the Vampire Slayer (Dark Horse, 1998), 2026-10-17: usenet's real #1–#3 were thrown out
as 'webtoon rips' — a 1998 scan is ~974px wide at a print page's 1.55 — and GetComics' BOOM!
2019 #2–#10 were taken in their place, ComicInfo saying 2019 and nothing asking. The webtoon
guard now judges issues from 2015 on; a ComicInfo year far from the release is another run;
a GetComics post dated to another run is skipped."""
import io
import zipfile

import pytest
from PIL import Image

from kometa.downloader import WrongIssueError, _verify_single_issue
from kometa.getcomics_client import _post_year_conflicts

OLD_SCAN = (974, 1510)        # Buffy.The.Vampire.Slayer.001.(1998).(Obi)
WEBTOON = (800, 1280)         # Absolute Superman #21 [digital-mobile]


def _cbz(path, dims, year=None, series="Buffy the Vampire Slayer", number=1, pages=6):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            buf = io.BytesIO()
            Image.new("RGB", dims, (40, 40, 40)).save(buf, "PNG")
            z.writestr(f"Buffy the Vampire Slayer {number:03d}-{i:03d}.png", buf.getvalue())
        if year:
            z.writestr("ComicInfo.xml", f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
                                        f"<Year>{year}</Year><Publisher>BOOM! Studios</Publisher></ComicInfo>")
    return str(path)


def test_a_1998_scan_is_not_a_webtoon(tmp_path):
    f = _cbz(tmp_path / "Buffy.The.Vampire.Slayer.001.1998.cbz", OLD_SCAN)
    _verify_single_issue(f, 1.0, series_title="Buffy the Vampire Slayer", released="1998-09-23")


def test_a_recent_webtoon_chapter_is_still_refused(tmp_path):
    f = _cbz(tmp_path / "Absolute Superman 021.cbz", WEBTOON)
    with pytest.raises(WrongIssueError, match="webtoon"):
        _verify_single_issue(f, 1.0, released="2025-09-10")


def test_with_no_release_date_the_webtoon_guard_still_judges(tmp_path):
    f = _cbz(tmp_path / "Saga 001.cbz", WEBTOON)
    with pytest.raises(WrongIssueError, match="webtoon"):
        _verify_single_issue(f, 1.0)


def test_comicinfo_dated_to_another_run_is_refused(tmp_path):
    f = _cbz(tmp_path / "Buffy the Vampire Slayer 004.cbz", (1988, 3057), year=2019, number=4)
    with pytest.raises(WrongIssueError, match="another run"):
        _verify_single_issue(f, 4.0, series_title="Buffy the Vampire Slayer", released="1998-12-30")
    # the same file for BOOM!'s own #4 is fine; and a cover year a year on from the release is fine
    _verify_single_issue(f, 4.0, series_title="Buffy the Vampire Slayer", released="2019-04-24")
    _verify_single_issue(f, 4.0, series_title="Buffy the Vampire Slayer", released="2018-11-28")


def test_a_getcomics_post_dated_to_another_run_conflicts():
    assert _post_year_conflicts("Buffy The Vampire Slayer #4 (2019)", 1998)
    assert not _post_year_conflicts("Buffy The Vampire Slayer #4 (1998)", 1998)
    assert not _post_year_conflicts("Buffy The Vampire Slayer #63 (2003)", 2002)     # a year either way
    assert not _post_year_conflicts("Buffy The Vampire Slayer #4", 1998)              # no year said: innocent
    assert not _post_year_conflicts("Buffy The Vampire Slayer #4 (2019)", None)      # no date known: innocent
