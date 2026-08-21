"""Pull list, Activity, Settings — each view renders its seeded data."""
from playwright.sync_api import expect


def test_pull_list_groups_upcoming(app):
    app.locator('.nav-item[data-view="pull-list"]').click()
    expect(app.locator(".pull-group-label").first).to_be_visible()
    row = app.locator(".pull-row", has_text="Test Comic Alpha")
    expect(row.first).to_be_visible()          # issue #4 is on the pull list


def test_activity_shows_queue_states(app):
    # Terminal states land in the Completed section as .act-row (in-flight
    # ones are .act-card) — the seed is all-terminal so boot recovery and the
    # queue processor leave it alone.
    app.locator('.nav-item[data-view="activity"]').click()
    expect(app.locator(".act-row")).to_have_count(2)
    expect(app.get_by_text("Failed").first).to_be_visible()
    expect(app.get_by_text("Not Found").first).to_be_visible()


def test_activity_reconciles_rows_in_place(app):
    # The motion contract: a state change morphs the EXISTING row (same DOM node,
    # same <img> — no cover-in replay), a section hop collapses one side and grows
    # the other, and a vanished item collapses out. Drive _reconcileActivity with
    # synthetic queues and check the DOM keeps its identity.
    app.locator('.nav-item[data-view="activity"]').click()
    expect(app.locator(".act-row")).to_have_count(2)
    result = app.evaluate(
        """async () => {
      const rows = [...document.querySelectorAll('.act-row[data-qid]')];
      const [a, b] = rows.map(r => +r.dataset.qid);
      const row = (id, state, extra = {}) => Object.assign({
        id, state, title: 'T', publisher: 'P', kind: 'issue', issue_number: 3,
        tracked_series_id: 1, error: null, meta_json: null, progress: null,
      }, extra);
      // 1) same-section morph: failed -> done. Row + img must SURVIVE.
      const el = document.querySelector(`.act-row[data-qid="${a}"]`);
      const img = el.querySelector('img');
      img.__kept = true;
      await _reconcileActivity([row(a, 'done'), row(b, 'not_found')]);
      const el2 = document.querySelector(`.act-row[data-qid="${a}"]`);
      const morph = {
        sameNode: el2 === el,
        sameImg: !!(el2.querySelector('img') && el2.querySelector('img').__kept),
        qstate: el2.dataset.qstate,
        dimmed: el2.classList.contains('done'),
      };
      // 2) section hop (a -> downloading) + removal (b gone).
      await _reconcileActivity([row(a, 'downloading', { progress: { done: 50, total: 100 } })]);
      return Object.assign(morph, {
        count: document.querySelectorAll('.act-row[data-qid]').length,
        hopped: !!document.querySelector('.act-section[data-sec="progress"] .act-row[data-qid="' + a + '"]'),
        pct: (document.getElementById('actfill-' + a) || {style:{}}).style.width,
      });
    }"""
    )
    assert result["sameNode"] and result["sameImg"], "morph must not recreate the row/img"
    assert result["qstate"] == "done" and result["dimmed"]
    assert result["count"] == 1 and result["hopped"]
    assert result["pct"] == "50%"


def test_settings_renders_and_autosaves(app):
    app.locator('.nav-item[data-view="settings"]').click()
    field = app.locator("#ff-root")            # the aligned folder field's input
    expect(field).to_be_visible()
    # The autosave REALLY saves — this test mutates shared session config, so
    # the new value must be a directory that exists or comics_root_ok flips
    # false and the wizard test (which runs after) gets the setup screen.
    with app.expect_request(
        lambda r: "/api/config" in r.url and r.method == "PATCH", timeout=10000
    ):
        field.fill("/tmp")
        field.blur()


def test_source_toggles_render_and_flip(app):
    # The Usenet/Torrents search-source toggles: default-on (seed sets no flag),
    # and flipping one PATCHes the config + dims the section.
    app.locator('.nav-item[data-view="settings"]').click()
    usenet = app.locator('#t-usenet')
    expect(usenet).to_be_checked()                       # absent flag = enabled
    with app.expect_request(
        lambda r: '/api/config' in r.url and r.method == 'PATCH', timeout=10000
    ):
        usenet.uncheck()
    # section collapses (fold + fade)
    expect(app.locator('#sec-usenet.section-off')).to_have_count(1)
