"""The other half of an acquisition: after a file lands, wait for Komga to list
it, re-ask for a scan if the first request got swallowed, then sync the one
series so komga_book_id (read link + thumbnail) gets stamped now, not at the
next scheduled sync. Everything injected — no clock, no network, no Komga."""
import kometa.acquisition as acq
import kometa.db as db


class FakeKomga:
    """Lists nothing until `appear_after` polls have happened."""
    def __init__(self, appear_after: int, filename="Saga #001.cbz", deleted=False):
        self.appear_after = appear_after
        self.polls = 0
        self.filename = filename
        self.deleted = deleted

    def get_books(self, series_id):
        self.polls += 1
        if self.polls < self.appear_after:
            return []
        return [{"id": "B1", "name": self.filename.rsplit(".", 1)[0],
                 "url": f"/comics/Image/Saga/{self.filename}", "deleted": self.deleted}]


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _linked_series(db_path, monkeypatch):
    sid = db.add_series(komga_series_id="K1", title="Saga", publisher="Image",
                        year_began=2012, folder_path="/comics/Image/Saga",
                        on_pull_list=True, path=db_path)
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    return sid


def test_syncs_once_komga_lists_the_file(db_path, monkeypatch):
    sid = _linked_series(db_path, monkeypatch)
    komga = FakeKomga(appear_after=3)
    clock = FakeClock()
    synced, scans = [], []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: scans.append(1),
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is True
    assert synced == [sid]
    assert komga.polls == 3
    assert scans == []                      # appeared before the rescan threshold
    assert clock.t == 2 * acq._RESYNC_POLL_S


def test_rescan_once_when_the_first_scan_request_was_swallowed(db_path, monkeypatch):
    sid = _linked_series(db_path, monkeypatch)
    # Appears only well after the rescan threshold.
    polls_to_appear = (acq._RESYNC_RESCAN_AT_S // acq._RESYNC_POLL_S) + 4
    komga = FakeKomga(appear_after=polls_to_appear)
    clock = FakeClock()
    synced, scans = [], []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: scans.append(1),
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is True
    assert scans == [1]                     # exactly one re-request, not one per poll
    assert synced == [sid]


def test_gives_up_at_deadline_and_still_syncs(db_path, monkeypatch):
    sid = _linked_series(db_path, monkeypatch)
    komga = FakeKomga(appear_after=10_000)
    clock = FakeClock()
    synced, scans = [], []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: scans.append(1),
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is False
    assert synced == [sid]                  # ownership is folder-truth; sync anyway
    assert scans == [1]
    assert clock.t >= acq._RESYNC_DEADLINE_S


def test_soft_deleted_twin_does_not_count(db_path, monkeypatch):
    sid = _linked_series(db_path, monkeypatch)
    komga = FakeKomga(appear_after=1, deleted=True)
    clock = FakeClock()
    synced = []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: None,
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is False
    assert synced == [sid]


def test_unlinked_series_waits_blind_then_syncs(db_path, monkeypatch):
    sid = db.add_series(komga_series_id=None, title="Saga", publisher="Image",
                        year_began=2012, folder_path=None, on_pull_list=True, path=db_path)
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    komga = FakeKomga(appear_after=1)
    clock = FakeClock()
    synced = []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: None,
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is False
    assert komga.polls == 0                 # nothing to poll without a Komga series id
    assert clock.sleeps == [acq._RESYNC_BLIND_WAIT_S]
    assert synced == [sid]


def test_poll_error_does_not_abort(db_path, monkeypatch):
    sid = _linked_series(db_path, monkeypatch)

    class Flaky(FakeKomga):
        def get_books(self, series_id):
            self.polls += 1
            if self.polls == 1:
                raise ConnectionError("komga mid-restart")
            return super().get_books(series_id)

    komga = Flaky(appear_after=2)
    clock = FakeClock()
    synced = []
    seen = acq._resync_worker(sid, "/comics/Image/Saga/Saga #001.cbz", komga=komga,
                              sync_fn=lambda s: synced.append(s["id"]),
                              scan_fn=lambda: None,
                              sleep_fn=clock.sleep, now_fn=clock.now)
    assert seen is True
    assert synced == [sid]


def test_unknown_series_is_a_noop(db_path, monkeypatch):
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    synced = []
    assert acq._resync_worker(999, "/x.cbz", komga=FakeKomga(1),
                              sync_fn=lambda s: synced.append(1),
                              sleep_fn=lambda s: None) is False
    assert synced == []
