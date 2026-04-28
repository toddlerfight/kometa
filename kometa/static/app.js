// --- API ---

const api = {
  get(url) {
    return fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.status === 204 ? null : r.json(); });
  },
  patch(url, body) {
    return fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  del(url) {
    return fetch(url, { method: 'DELETE' }).then(r => { if (!r.ok) throw new Error(r.status); });
  },
};

// --- Router ---

let currentView = 'series';
let currentParams = {};
let detailTab = 'all';

function navigate(view, params = {}) {
  currentView = view;
  currentParams = params;
  if (view !== 'series-detail') detailTab = 'all';
  updateNav();
  renderView();
}

function updateNav() {
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === currentView);
  });
}

function setTopbar(actionsHTML = '') {
  document.getElementById('topbar-title').textContent = '';
  document.getElementById('topbar-actions').innerHTML = actionsHTML;
}

function setApp(html) {
  document.getElementById('app').innerHTML = html;
}

function renderView() {
  switch (currentView) {
    case 'series':        return renderSeries();
    case 'series-detail': return renderSeriesDetail(currentParams.id);
    case 'pull-list':     return renderPullList();
    case 'activity':      return renderActivity();
    case 'wanted':        return renderWanted();
    case 'settings':      return renderSettings();
    default:              setApp('<div class="state-msg">Not found</div>');
  }
}

// --- Helpers ---

function esc(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtNum(n) {
  const f = parseFloat(n);
  return Number.isInteger(f) ? String(f) : String(f);
}

function issueStatus(issue) {
  const today = new Date().toISOString().slice(0, 10);
  if (issue.in_komga) return 'owned';
  if (!issue.store_date) return 'unknown';
  return issue.store_date > today ? 'upcoming' : 'missing';
}

function fmtDayDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-AU', { weekday: 'long', month: 'short', day: 'numeric' });
}

function relativeTime(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.floor(hrs / 24);
  return `${days} day${days > 1 ? 's' : ''} ago`;
}

function pullGroup(isoDate) {
  const today = new Date(); today.setHours(0,0,0,0);
  const d = new Date(isoDate + 'T00:00:00');
  const dow = today.getDay();
  const weekStart = new Date(today); weekStart.setDate(today.getDate() - dow);
  const nextWeekStart = new Date(weekStart); nextWeekStart.setDate(weekStart.getDate() + 7);
  const nextWeekEnd   = new Date(nextWeekStart); nextWeekEnd.setDate(nextWeekStart.getDate() + 7);
  if (d < nextWeekStart) return 'This Week';
  if (d < nextWeekEnd)   return 'Next Week';
  return 'Later';
}

// progress bar color: green=100%, lime≥90%, amber otherwise
function barColor(owned, total) {
  if (!total) return 'var(--su3)';
  const pct = owned / total;
  if (pct >= 1)   return 'var(--grn)';
  if (pct >= 0.9) return 'var(--pri)';
  return 'var(--amb)';
}

function countColor(owned, total) {
  if (!total) return 'var(--tq)';
  return owned >= total ? 'var(--grn)' : 'var(--tm)';
}

// --- Series List (poster grid) ---

async function renderSeries() {
  setTopbar(`<button class="btn btn-ghost" onclick="syncAll(this)">Sync All</button>
    <button class="btn btn-primary" onclick="showAddSeries()">+ Add Series</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const series = await api.get('/api/series');

  if (!series.length) {
    setApp('<div class="state-msg">No series tracked yet.</div>');
    return;
  }

  const cards = series.map(s => {
    const total = s.owned + s.missing;
    const pct = total > 0 ? (s.owned / total) * 100 : 0;
    const color = barColor(s.owned, total);
    const cc = countColor(s.owned, total);
    const pub = s.publisher ? s.publisher.toUpperCase() : '';
    return `
      <div class="series-card" tabindex="0" role="button" onclick="navigate('series-detail', {id: ${s.id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <img class="series-card-cover" src="/api/series/${s.id}/thumbnail" alt="${esc(s.title)}"
          onerror="this.style.opacity='0.15'">
        <div class="series-card-bar-track">
          <div class="series-card-bar-fill" style="width:${pct}%;background:${color}"></div>
        </div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(s.title)}</div>
          <div class="series-card-count" style="color:${cc}">${s.owned}/${total}</div>
        </div>
        <div class="series-card-publisher">${esc(pub)}</div>
      </div>
    `;
  }).join('');

  setApp(`<div class="series-grid">${cards}</div>`);
}

async function syncAll(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing...'; }
  try {
    await api.post('/api/sync', {});
    await renderSeries();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Sync All'; }
  }
}

async function syncSeries(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    await api.post(`/api/sync/${id}`, {});
    if (currentView === 'series-detail' && currentParams.id === id) {
      renderSeriesDetail(id);
    } else {
      renderSeries();
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Sync'; }
  }
}

// --- Series Detail ---

async function renderSeriesDetail(id) {
  setTopbar(`<button class="btn btn-ghost btn-sm" onclick="navigate('series')">← Back</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const s = await api.get(`/api/series/${id}`);
  const meta = [s.publisher ? s.publisher.toUpperCase() : '', s.year_began].filter(Boolean).join('  •  ');
  const total = s.owned + s.missing + s.upcoming;

  const statsLine = [total + ' issues tracked',
    s.owned    ? s.owned + ' owned' : '',
    s.missing  ? s.missing + ' missing' : '',
    s.upcoming ? s.upcoming + ' upcoming' : '',
  ].filter(Boolean).join('  •  ');

  const chips = [
    s.owned    ? `<span class="chip chip-owned">${s.owned} owned</span>` : '',
    s.missing  ? `<span class="chip chip-missing">${s.missing} missing</span>` : '',
    s.upcoming ? `<span class="chip chip-upcoming">${s.upcoming} upcoming</span>` : '',
  ].filter(Boolean).join('');

  const pullBtn = s.on_pull_list
    ? `<button class="btn btn-ghost btn-sm" onclick="togglePullList(${s.id}, false)">Remove from Pull List</button>`
    : `<button class="btn btn-primary btn-sm" onclick="togglePullList(${s.id}, true)">+ Pull List</button>`;

  const tabs = ['all','owned','missing','upcoming'].map(t => `
    <div class="issue-tab ${detailTab === t ? 'active' : ''}" onclick="setDetailTab('${t}', ${id})">${t}</div>
  `).join('');

  const filtered = s.issues.filter(i => {
    const st = issueStatus(i);
    if (detailTab === 'owned')    return st === 'owned';
    if (detailTab === 'missing')  return st === 'missing';
    if (detailTab === 'upcoming') return st === 'upcoming';
    return true;
  });

  const tiles = filtered.map(issue => {
    const st = issueStatus(issue);
    const num = `#${fmtNum(issue.number)}`;
    let inner = '';
    if (st === 'owned') {
      inner = `<div class="issue-tile-img">
        <img src="/api/book/${issue.komga_book_id}/thumbnail" alt="${num}" loading="lazy"
          onerror="this.parentElement.classList.add('unknown');this.remove()">
      </div>`;
    } else {
      inner = `<div class="issue-tile-img ${st}"></div>`;
    }
    return `<div class="issue-tile" title="${esc(s.title)} ${num}">
      ${inner}
      <div class="issue-tile-num">${num}</div>
    </div>`;
  }).join('');

  setApp(`
    <div class="detail-band">
      <img class="detail-thumb" src="/api/series/${s.id}/thumbnail" alt="${esc(s.title)}"
        onerror="this.style.opacity='0.2'">
      <div class="detail-info">
        <div class="detail-title">${esc(s.title)}</div>
        <div class="detail-meta">${esc(meta)}</div>
        <div class="detail-stats-line">${esc(statsLine)}</div>
        <div class="detail-chips">${chips}</div>
      </div>
    </div>
    <div class="detail-actions-row">
      ${pullBtn}
      <button class="btn btn-ghost btn-sm" onclick="syncSeries(${s.id}, this)">Sync</button>
      <button class="btn btn-danger btn-sm" onclick="confirmDelete(${s.id}, '${esc(s.title)}')">Remove</button>
    </div>
    <div class="issue-tabs">${tabs}</div>
    <div class="issue-grid">${tiles || '<div class="state-msg" style="grid-column:1/-1">Nothing here.</div>'}</div>
  `);
}

function setDetailTab(tab, id) {
  detailTab = tab;
  renderSeriesDetail(id);
}

async function togglePullList(id, on) {
  await api.patch(`/api/series/${id}/pull-list`, { on_pull_list: on });
  renderSeriesDetail(id);
}

function confirmDelete(id, title) {
  showModal(`
    <div class="modal-title">Remove Series</div>
    <div class="confirm-body">
      Remove <strong style="color:var(--tp)">${esc(title)}</strong> from tracking?
      <div class="confirm-note">Your Komga library is not affected.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="doDelete(${id})">Remove</button>
    </div>
  `);
}

async function doDelete(id) {
  closeModal();
  await api.del(`/api/series/${id}`);
  navigate('series');
}

// --- Pull List ---

async function renderPullList() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const [items, allSeries] = await Promise.all([
    api.get('/api/pull-list?days=180'),
    api.get('/api/series'),
  ]);

  if (!items.length) {
    setApp('<div class="page-title">Pull List</div><div class="state-msg">Nothing upcoming on your pull list.</div>');
    return;
  }

  const seriesMap = {};
  for (const s of allSeries) seriesMap[s.title] = s.id;

  const groups = { 'This Week': [], 'Next Week': [], 'Later': [] };
  for (const item of items) groups[pullGroup(item.store_date)].push(item);

  const html = Object.entries(groups)
    .filter(([, entries]) => entries.length > 0)
    .map(([label, entries]) => `
      <div class="pull-group">
        <div class="pull-group-label">${label.toUpperCase()}</div>
        ${entries.map(e => {
          const sid = seriesMap[e.title];
          const thumb = sid ? `/api/series/${sid}/thumbnail` : '';
          return `
            <div class="pull-row" ${sid ? `tabindex="0" role="button" onclick="navigate('series-detail', {id: ${sid}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${sid}})"` : ''}>
              ${thumb
                ? `<img class="pull-thumb" src="${thumb}" alt="" onerror="this.style.opacity='0.2'">`
                : `<div class="pull-thumb"></div>`}
              <div class="pull-series">${esc(e.title)}</div>
              <div class="pull-issue">Issue #${fmtNum(e.number)}</div>
              <div class="pull-date">${fmtDayDate(e.store_date)}</div>
            </div>
          `;
        }).join('')}
      </div>
    `).join('');

  setApp(`<div class="page-title">Pull List</div>${html}`);
}

// --- Activity ---

async function renderActivity() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const series = await api.get('/api/series');
  const synced = series
    .filter(s => s.last_synced)
    .sort((a, b) => b.last_synced.localeCompare(a.last_synced));

  if (!synced.length) {
    setApp('<div class="page-title">Activity</div><div class="state-msg">No sync activity yet.</div>');
    return;
  }

  const rows = synced.map(s => {
    const hasNew = (s.missing + s.upcoming) > 0;
    const evClass = hasNew ? 'ev-sync-ok' : 'ev-sync-none';
    const detail = hasNew
      ? `${s.title} — ${s.owned} owned`
      : `${s.title} — no new issues`;
    return `
      <div class="activity-row ${evClass}" tabindex="0" role="button" onclick="navigate('series-detail', {id: ${s.id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="activity-event">Sync complete</div>
        <div class="activity-detail">${esc(detail)}</div>
        <div class="activity-time">${relativeTime(s.last_synced)}</div>
      </div>
    `;
  }).join('');

  setApp(`
    <div class="page-title">Activity</div>
    <div class="activity-header">
      <div>Event</div><div>Detail</div><div class="ah-time">Time</div>
    </div>
    ${rows}
  `);
}

// --- Wanted ---

async function renderWanted() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const series = await api.get('/api/series');
  const today = new Date().toISOString().slice(0, 10);
  const rows = [];

  for (const s of series.filter(s => s.missing > 0)) {
    const detail = await api.get(`/api/series/${s.id}`);
    const missing = detail.issues.filter(i => !i.in_komga && i.store_date && i.store_date <= today);
    for (const iss of missing) rows.push({ s, iss });
  }

  if (!rows.length) {
    setApp('<div class="page-title">Wanted</div><div class="state-msg">Collection complete — nothing missing.</div>');
    return;
  }

  const html = rows.map(({ s, iss }) => `
    <div class="pull-row" tabindex="0" role="button" onclick="navigate('series-detail', {id: ${s.id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
      <img class="pull-thumb" src="/api/series/${s.id}/thumbnail" alt="" onerror="this.style.opacity='0.2'">
      <div class="pull-series">${esc(s.title)}</div>
      <div class="pull-issue">Issue #${fmtNum(iss.number)}</div>
      <div class="pull-date" style="color:var(--amb)">${iss.store_date || ''}</div>
    </div>
  `).join('');

  setApp(`<div class="page-title">Wanted</div>${html}`);
}

// --- Settings ---

function renderSettings() {
  setTopbar();
  setApp(`
    <div class="page-title">Settings</div>
    <div class="settings-grid">
      <div class="settings-card">
        <div class="settings-card-header">Komga</div>
        <div class="settings-field">
          <div class="settings-field-label">Server URL</div>
          <input class="settings-input" id="s-komga-url" value="http://192.168.1.166:8585">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Username</div>
          <input class="settings-input" id="s-komga-user" value="admin">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Password</div>
          <input class="settings-input" id="s-komga-pass" type="password" value="••••••••••">
        </div>
      </div>
      <div>
        <div class="settings-card">
          <div class="settings-card-header">Metron</div>
          <div class="settings-field">
            <div class="settings-field-label">Username</div>
            <input class="settings-input" id="s-metron-user" value="beakers">
          </div>
          <div class="settings-field">
            <div class="settings-field-label">Password</div>
            <input class="settings-input" id="s-metron-pass" type="password" value="••••••••••">
          </div>
        </div>
        <div class="settings-card" style="margin-top:24px">
          <div class="settings-card-header">Sync Schedule</div>
          <div class="settings-field">
            <div class="settings-field-label">Hours (24h)</div>
            <input class="settings-input" id="s-sync-hours" value="5, 12, 17">
          </div>
          <div class="settings-field">
            <div class="settings-field-label">Timezone</div>
            <input class="settings-input" id="s-timezone" value="Australia/Sydney">
          </div>
        </div>
      </div>
      <div class="settings-footer">
        <button class="btn btn-primary" onclick="saveSettings(this)">Save Settings</button>
      </div>
    </div>
  `);
}

function saveSettings(btn) {
  btn.disabled = true; btn.textContent = 'Saved';
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Save Settings'; }, 1500);
}

// --- Add Series Modal ---

let addState = { komga: null, metron: null };

function showAddSeries() {
  addState = { komga: null, metron: null };
  showModal(`
    <div class="modal-title">Add Series</div>
    <div class="step-label">Step 1 — Select from Komga</div>
    <input class="search-input" id="komga-q" placeholder="Search your Komga library..." oninput="searchKomga(this.value)">
    <div class="search-results" id="komga-results"></div>
    <div id="step2" class="hidden" style="margin-top:20px">
      <div class="step-label">Step 2 — Match on Metron</div>
      <input class="search-input" id="metron-q" placeholder="Search Metron..." oninput="searchMetron(this.value)">
      <div class="search-results" id="metron-results"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="add-btn" disabled onclick="submitAdd()">Add Series</button>
    </div>
  `);
  setTimeout(() => document.getElementById('komga-q')?.focus(), 50);
}

let komgaTimer, metronTimer;

function searchKomga(q) {
  clearTimeout(komgaTimer);
  const el = document.getElementById('komga-results');
  if (!el || q.length < 2) { if (el) el.innerHTML = ''; return; }
  komgaTimer = setTimeout(async () => {
    const results = await api.get(`/api/search/komga?q=${encodeURIComponent(q)}`);
    if (!document.getElementById('komga-results')) return;
    document.getElementById('komga-results').innerHTML = results.slice(0, 10).map(r => `
      <div class="search-result" onclick="selectKomga(this, ${JSON.stringify(JSON.stringify(r))})">
        <div>
          <div class="search-result-title">${esc(r.name || r.metadata?.title || '')}</div>
          <div class="search-result-meta">${esc(r.metadata?.publisher || '')}${r.metadata?.startYear ? ' · ' + r.metadata.startYear : ''}</div>
        </div>
      </div>
    `).join('');
  }, 280);
}

function selectKomga(el, jsonStr) {
  const r = JSON.parse(jsonStr);
  addState.komga = r; addState.metron = null;
  document.querySelectorAll('#komga-results .search-result').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('step2').classList.remove('hidden');
  const mq = document.getElementById('metron-q');
  mq.value = r.name || r.metadata?.title || '';
  document.getElementById('add-btn').disabled = true;
  searchMetron(mq.value);
}

function searchMetron(q) {
  clearTimeout(metronTimer);
  const el = document.getElementById('metron-results');
  if (!el || q.length < 2) { if (el) el.innerHTML = ''; return; }
  metronTimer = setTimeout(async () => {
    const results = await api.get(`/api/search/metron?q=${encodeURIComponent(q)}`);
    if (!document.getElementById('metron-results')) return;
    document.getElementById('metron-results').innerHTML = results.slice(0, 10).map(r => `
      <div class="search-result" onclick="selectMetron(this, ${r.id})">
        <div>
          <div class="search-result-title">${esc(r.name || r.series_name || '')}</div>
          <div class="search-result-meta">${esc(r.publisher?.name || '')}${r.year_began ? ' · ' + r.year_began : ''}</div>
        </div>
      </div>
    `).join('');
  }, 280);
}

function selectMetron(el, id) {
  addState.metron = { id };
  document.querySelectorAll('#metron-results .search-result').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('add-btn').disabled = false;
}

async function submitAdd() {
  if (!addState.komga || !addState.metron) return;
  const btn = document.getElementById('add-btn');
  btn.disabled = true; btn.textContent = 'Adding...';
  try {
    await api.post('/api/series', { komga_id: addState.komga.id, metron_id: addState.metron.id });
    closeModal();
    navigate('series');
  } catch(e) {
    btn.disabled = false; btn.textContent = 'Add Series';
    alert('Failed to add series — check console.');
    console.error(e);
  }
}

// --- Modal ---

function showModal(html) {
  const modal = document.getElementById('modal');
  modal.innerHTML = html;
  modal.classList.remove('hidden');
  document.getElementById('modal-backdrop').classList.remove('hidden');
  // focus first focusable element
  const first = modal.querySelector('button, input, [tabindex]');
  if (first) setTimeout(() => first.focus(), 30);
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
  document.getElementById('modal-backdrop').classList.add('hidden');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('modal').classList.contains('hidden')) {
    closeModal();
  }
});

// --- Boot ---

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => navigate(el.dataset.view));
});

navigate('series');
