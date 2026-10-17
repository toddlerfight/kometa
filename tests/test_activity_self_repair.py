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


def test_range_enclosed_scoring():
    """Single issue searches should score releases that encloses them in a range (001-030)."""
    from kometa.usenet_client import _nzb_score

    title = "Buffy the Vampire Slayer Classic (001-030) (Digital)"
    series = "Buffy the Vampire Slayer"

    # Issues 1, 11, 20, 30 are inside 001-030
    assert _nzb_score(title, series, 1.0) >= 15
    assert _nzb_score(title, series, 11.0) >= 15
    assert _nzb_score(title, series, 20.0) >= 15
    assert _nzb_score(title, series, 30.0) >= 15

    # Issue 31 or 40 are NOT inside 001-030
    assert _nzb_score(title, series, 31.0) < 15
    assert _nzb_score(title, series, 40.0) < 15

    # Year range (1998-2003) is NOT treated as an issue range
    year_title = "Buffy the Vampire Slayer (1998-2003) Digital"
    assert _nzb_score(year_title, series, 11.0) < 15


def test_multi_issue_harvest_in_single_finalize(monkeypatch, db_path, tmp_path):
    """When a single issue row (#1) downloads a multi-issue release (001-030),

    finalize places all valid enclosed comics and closes their queue rows."""
    dest = tmp_path / "Buffy"
    dest.mkdir()
    s = db.add_series("Buffy", "Dark Horse", 1998, str(dest), on_pull_list=True, path=db_path)

    # Queue issue #1 (downloading) and #2 (failed)
    db.queue_issue(s, 1.0, db_path)
    db.queue_issue(s, 2.0, db_path)
    with db._connect(db_path) as conn:
        q1 = conn.execute("SELECT id FROM download_queue WHERE tracked_series_id=? AND issue_number=1.0", (s,)).fetchone()[0]
        conn.execute("UPDATE download_queue SET state='failed', error='incomplete: articles missing' WHERE tracked_series_id=? AND issue_number=2.0", (s,))

    # Staging download containing files for #1 and #2
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
    # Issue #2 was harvested as a bonus from the multi-issue download!
    assert owned.get(2.0) == 1
    assert q_rows[2.0] == ("done", "On the shelf")
