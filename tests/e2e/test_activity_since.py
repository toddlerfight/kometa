"""Activity merges /api/queue?since= answers instead of fetching the whole queue
every 2 s — and starts over whenever a merge could be wrong."""


def test_activity_merges_changes_drops_deleted_rows_and_refetches_on_a_quiet_state_change(app):
    app.locator('.nav-item[data-view="activity"]').click()
    out = app.evaluate(
        """async () => {
      const row = (id, state, t, extra = {}) => Object.assign({ id, state, updated_at: t, title: 'T',
        kind: 'issue', issue_number: id, tracked_series_id: 1, error: null, meta_json: null }, extra);
      // the server's truth, and what a ?since= answer would carry
      let truth = [row(1, 'done', '2026-10-17 01:00:00'), row(2, 'downloading', '2026-10-17 02:00:00')];
      const calls = [];
      const real = api.get;
      api.get = async (url) => {
        if (!url.startsWith('/api/queue?since=')) return real(url);
        const since = decodeURIComponent(url.split('since=')[1]);
        calls.push(since);
        const live = ['downloading', 'queued', 'via_trade'];
        return { at: '2026-10-17 03:00:00', states: truth.map(q => [q.id, q.state]),
                 rows: truth.filter(q => live.includes(q.state) || q.updated_at >= since) };
      };
      try {
        _actRows = null;
        const first = (await _fetchQueue()).map(q => q.id);
        // progress moves on the live row; nothing else is sent
        truth[1] = row(2, 'downloading', '2026-10-17 02:00:00', { progress: { done: 5, total: 10 } });
        const second = await _fetchQueue();
        // #1 changes state with no new time (a trade covering a stuck single does this): the merge must notice
        truth[0] = row(1, 'failed', '2026-10-17 01:00:00');
        const third = await _fetchQueue();
        // #2 is deleted
        truth = [truth[0]];
        const fourth = (await _fetchQueue()).map(q => q.id);
        return { first, progress: second.find(q => q.id === 2).progress.done,
                 quiet: third.find(q => q.id === 1).state, fourth, calls };
      } finally {
        api.get = real;
        _actRows = null;
      }
    }"""
    )
    assert out["first"] == [2, 1]                                   # newest first
    assert out["progress"] == 5
    assert out["quiet"] == "failed"                                 # caught by [id, state], refetched whole
    assert out["fourth"] == [1]
    assert out["calls"][0] == "" and out["calls"][1] == "2026-10-17 03:00:00"
    assert out["calls"][2:4] == ["2026-10-17 03:00:00", ""]         # the mismatch started over
