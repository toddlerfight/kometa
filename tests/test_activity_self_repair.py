"""Activity queue self-repair tests:
Dead rows (failed / not_found) whose issues are now on the shelf (via trade,
manual file drop, or complete pack) self-repair to 'done' ('On the shelf')
without leaving phantom failure alerts in Activity.
"""
import io
import zipfile
import kometa.db as db
import kometa.acquisition as acq
from kometa.main import get_queue


def _make_dummy_cbz(path, pages=5):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(pages):
            z.writestr(f"p{i:03d}.jpg", b"\xff\xd8\xff\xe0" + b"page" * 64)
    path.write_bytes(buf.getvalue())
    return str(path)


def test_close_rows_now_owned_global(db_path, tmp_path):
    """Calling close_rows_now_owned without a series_id heals dead rows across all series."""
    s1 = db.add_series("Series One", "Publisher", 2020, str(tmp_path / "s1"), on_pull_list=True, path=db_path)
    s2 = db.add_series("Series Two", "Publisher", 2020, str(tmp_path / "s2"), on_pull_list=True, path=db_path)

    db.queue_issue(s1, 1.0, db_path)
    db.queue_issue(s1, 2.0, db_path)
    db.queue_issue(s2, 10.0, db_path)
    db.queue_issue(s2, 11.0, db_path)

    with db._connect(db_path) as conn:
        q1 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=1.0", (s1,)).fetchone()[0]
        q2 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=2.0", (s1,)).fetchone()[0]
        q3 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=10.0", (s2,)).fetchone()[0]
        q4 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=11.0", (s2,)).fetchone()[0]

        conn.execute("UPDATE download_queue SET state='failed', error='Articles missing' WHERE id=?", (q1,))
        conn.execute("UPDATE download_queue SET state='not_found', error='Not on GetComics' WHERE id=?", (q3,))
        conn.execute("UPDATE download_queue SET state='downloading' WHERE id=?", (q2,))
        conn.execute("UPDATE download_queue SET state='failed', error='Corrupt page' WHERE id=?", (q4,))

        # s1 #1 is on shelf, s2 #10 is on shelf, s1 #2 is on shelf (but downloading), s2 #11 is NOT on shelf
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 1.0, 1)", (s1,))
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 10.0, 1)", (s2,))
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 2.0, 1)", (s1,))
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 11.0, 0)", (s2,))

    # Global close across all series
    healed = db.close_rows_now_owned(path=db_path)
    assert healed == 2  # q1 and q3

    with db._connect(db_path) as conn:
        rows = {r["id"]: (r["state"], r["error"]) for r in conn.execute("SELECT id, state, error FROM download_queue")}

    assert rows[q1] == ("done", "On the shelf")
    assert rows[q3] == ("done", "On the shelf")
    assert rows[q2][0] == "downloading"  # active row untouched
    assert rows[q4][0] == "failed"       # unowned issue stays failed


def test_get_queue_self_repairs_dead_rows(monkeypatch, db_path, tmp_path):
    """GET /api/queue automatically heals dead rows for owned issues before returning."""
    monkeypatch.setattr("kometa.main.DB_PATH", db_path)
    s = db.add_series("Buffy", "Dark Horse", 1998, str(tmp_path / "buffy"), on_pull_list=True, path=db_path)
    db.queue_issue(s, 10.0, db_path)

    with db._connect(db_path) as conn:
        qid = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=10.0", (s,)).fetchone()[0]
        conn.execute("UPDATE download_queue SET state='failed', error='incomplete: 4 articles missing' WHERE id=?", (qid,))
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 10.0, 1)", (s,))

    # When get_queue runs, it should close the row and return it as done
    items = get_queue()
    row = next(i for i in items if i["id"] == qid)
    assert row["state"] == "done"
    assert row["error"] == "On the shelf"


def test_sweep_missing_self_repairs_dead_rows(monkeypatch, db_path, tmp_path):
    """_sweep_missing heals dead rows whose issues have since landed on the shelf."""
    s = db.add_series("Buffy", "Dark Horse", 1998, str(tmp_path / "buffy"), on_pull_list=True, path=db_path)
    db.queue_issue(s, 20.0, db_path)

    with db._connect(db_path) as conn:
        qid = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=20.0", (s,)).fetchone()[0]
        conn.execute("UPDATE download_queue SET state='not_found', error='Not on GetComics' WHERE id=?", (qid,))
        conn.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, 20.0, 1)", (s,))

    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "_prowlarr", lambda: None)
    monkeypatch.setattr(acq, "_sabnzbd", lambda: None)
    acq._sweep_missing()

    with db._connect(db_path) as conn:
        row = dict(conn.execute("SELECT state, error FROM download_queue WHERE id=?", (qid,)).fetchone())
    assert row["state"] == "done"
    assert row["error"] == "On the shelf"


def test_pack_finalize_rescan_closes_dead_rows(monkeypatch, db_path, tmp_path):
    """When a pack sentinel lands, it immediately rescans the folder and closes dead single-issue rows."""
    dest = tmp_path / "Buffy"
    dest.mkdir()
    s = db.add_series("Buffy", "Dark Horse", 1998, str(dest), on_pull_list=True, path=db_path)

    # Failed single issue rows exist in queue
    db.queue_issue(s, 1.0, db_path)
    db.queue_issue(s, 2.0, db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE download_queue SET state='failed', error='incomplete: articles missing' WHERE tracked_series_id=? AND issue_number=1.0", (s,))
        conn.execute("UPDATE download_queue SET state='not_found', error='not found' WHERE tracked_series_id=? AND issue_number=2.0", (s,))
        # Pack sentinel
        pack_qid = conn.execute("INSERT INTO download_queue (tracked_series_id, issue_number, kind, state) VALUES (?, -1, 'issue', 'processing')", (s,)).lastrowid

    # Staging download with completed pack files
    stage = tmp_path / "stage_pack"
    stage.mkdir()
    _make_dummy_cbz(stage / "Buffy the Vampire Slayer #001.cbz")
    _make_dummy_cbz(stage / "Buffy the Vampire Slayer #002.cbz")

    monkeypatch.setattr(acq, "DB_PATH", db_path)
    item = {"id": pack_qid, "tracked_series_id": s, "issue_number": -1, "title": "Buffy", "folder_path": str(dest)}
    acq._finalize_download(item, pack_qid, str(stage), label="Usenet", keep_source=False)

    with db._connect(db_path) as conn:
        q_rows = {r["issue_number"]: (r["state"], r["error"]) for r in conn.execute("SELECT issue_number, state, error FROM download_queue WHERE tracked_series_id=?", (s,))}
        owned = {r["number"]: r["owned"] for r in conn.execute("SELECT number, owned FROM issue_status WHERE tracked_series_id=?", (s,))}

    assert q_rows[-1][0] == "done"
    assert owned[1.0] == 1
    assert owned[2.0] == 1
    assert q_rows[1.0] == ("done", "On the shelf")
    assert q_rows[2.0] == ("done", "On the shelf")


def test_strict_series_title_matching():
    """Extra content words after the series name indicate a different series and must score 0."""
    from kometa.usenet_client import _nzb_score, _pack_score

    series = "Buffy the Vampire Slayer"

    # Spinoff / distinct series with extra content words after series name: rejected
    assert _nzb_score("Buffy the Vampire Slayer Classic (001-030) (Digital)", series, 1.0) == 0
    assert _nzb_score("Buffy the Vampire Slayer Classic #001", series, 1.0) == 0
    assert _nzb_score("The Walking Dead Deluxe #1", "The Walking Dead", 1.0) == 0
    assert _nzb_score("Invincible Iron Man #1", "Invincible", 1.0) == 0
    assert _nzb_score("Batman Eternal #1", "Batman", 1.0) == 0

    # Legitimate releases with issue number, year, or Vol after series name: accepted
    assert _nzb_score("Buffy the Vampire Slayer #010 (1999) (Digital)", series, 10.0) == 15
    assert _nzb_score("Buffy the Vampire Slayer 010 (1999) (Digital) (Zone-Empire)", series, 10.0) == 15
    assert _nzb_score("Buffy the Vampire Slayer (1998) #010", series, 10.0) == 15
    assert _nzb_score("Buffy the Vampire Slayer Vol 2 005", series, 5.0) == 15

    # Pack scoring rejects spinoff packs like Classic
    assert _pack_score("Buffy the Vampire Slayer Classic (001-030)", series, 500_000_000) == 0
    # Pack scoring accepts legitimate complete run / range packs
    assert _pack_score("Buffy the Vampire Slayer (001-063) (Digital)", series, 500_000_000) > 10
    assert _pack_score("Buffy the Vampire Slayer Complete Collection", series, 500_000_000) > 10


def test_single_issue_finalize_only_places_target_issue(monkeypatch, db_path, tmp_path):
    """A single issue download (#1) places ONLY the targeted issue (#1) and does
    not blindly place unrelated sibling files."""
    dest = tmp_path / "Buffy"
    dest.mkdir()
    s = db.add_series("Buffy", "Dark Horse", 1998, str(dest), on_pull_list=True, path=db_path)

    # Queue issue #1 (downloading) and #2 (failed)
    db.queue_issue(s, 1.0, db_path)
    db.queue_issue(s, 2.0, db_path)
    with db._connect(db_path) as conn:
        q1 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=1.0", (s,)).fetchone()[0]
        conn.execute("UPDATE download_queue SET state='failed', error='incomplete: articles missing' WHERE tracked_series_id=? AND issue_number=2.0", (s,))

    # Staging download containing files for #1 and an extra file
    stage = tmp_path / "stage_single_multi"
    stage.mkdir()
    _make_dummy_cbz(stage / "Buffy the Vampire Slayer #001.cbz")
    _make_dummy_cbz(stage / "Buffy the Vampire Slayer #002.cbz")

    monkeypatch.setattr(acq, "DB_PATH", db_path)
    item = {"id": q1, "tracked_series_id": s, "issue_number": 1.0, "title": "Buffy", "folder_path": str(dest)}
    acq._finalize_download(item, q1, str(stage), label="Torrent", keep_source=False)

    with db._connect(db_path) as conn:
        q_rows = {r["issue_number"]: (r["state"], r["error"]) for r in conn.execute("SELECT issue_number, state, error FROM download_queue WHERE tracked_series_id=?", (s,))}
        owned = {r["number"]: r["owned"] for r in conn.execute("SELECT number, owned FROM issue_status WHERE tracked_series_id=?", (s,))}

    assert q_rows[1.0][0] == "done"
    assert owned.get(1.0) == 1
    # Issue #2 was NOT placed by single issue #1 finalize
    assert not owned.get(2.0)
    assert q_rows[2.0][0] == "failed"
