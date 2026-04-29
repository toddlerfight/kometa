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
  const navView = currentView === 'series-detail' ? 'library' : currentView;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === navView);
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
    case 'library':       return renderLibraryBrowse();
    case 'series-detail': return renderSeriesDetail(currentParams.id);
    case 'pull-list':     return renderPullList();
    case 'activity':      return renderActivity();
    case 'settings':      return renderSettings();
    case 'match-review':  return renderMatchReview();
    default:              setApp('<div class="state-msg">Not found</div>');
  }
}

// --- Helpers ---

function esc(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _fmtReleaseDate(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
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

// --- Series List ---

async function renderSeries() {
  setTopbar(`<button class="btn btn-ghost" onclick="syncAll(this)">Sync All</button>
    <button class="btn btn-primary" onclick="navigate('library-browse')">+ Add Series</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const series = await api.get('/api/series');

  if (!series.length) {
    setApp(`
      <div class="empty-state">
        <div class="empty-state-title">No series tracked yet</div>
        <div class="empty-state-body">Browse your Komga library to start tracking series.</div>
        <button class="btn btn-primary" onclick="navigate('library-browse')">Browse Library</button>
      </div>
    `);
    return;
  }

  const cards = series.map(s => {
    const total = s.owned + s.missing;
    const pct = total > 0 ? (s.owned / total) * 100 : 0;
    const color = barColor(s.owned, total);
    const cc = countColor(s.owned, total);
    const pub = s.publisher ? s.publisher.toUpperCase() : '';
    return `
      <div class="series-card" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="/api/series/${s.id}/thumbnail" alt="${esc(s.title)}"
            onerror="this.style.opacity='0.15'">
        </div>
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
    await _loadBrowsePage();
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

// --- Library Browse ---

let browseState = { search: '', searchTimer: null, filter: 'all', _cache: null };

async function renderLibraryBrowse() {
  document.getElementById('topbar-title').textContent = 'Library';
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-ghost btn-sm" onclick="syncAll(this)">Sync All</button>
    <button class="btn btn-primary btn-sm" onclick="navigate('match-review')">+ Add Series</button>
  `;
  browseState.search = '';
  browseState.filter = 'all';
  browseState._cache = null;
  setApp('<div class="state-msg">Loading...</div>');
  await _loadBrowsePage();
}

const BROWSE_FILTERS = [
  { key: 'all',      label: 'All' },
  { key: 'complete', label: 'Complete' },
  { key: 'partial',  label: 'Partial' },
  { key: 'missing',  label: 'Missing' },
];

function _browseFilterTabs() {
  return `<div class="browse-filters">
    ${BROWSE_FILTERS.map(f => `
      <button class="browse-filter-tab${browseState.filter === f.key ? ' active' : ''}"
        onclick="browseFilter('${f.key}')">${f.label}</button>
    `).join('')}
  </div>`;
}

function browseFilter(key) {
  browseState.filter = key;
  document.querySelectorAll('.browse-filter-tab').forEach(b =>
    b.classList.toggle('active', b.textContent.toLowerCase() === key)
  );
  _renderBrowseResults();
}

async function _loadBrowsePage() {
  const firstRender = !document.getElementById('browse-search');
  if (firstRender) {
    setApp(`
      <div class="browse-header">
        <input class="browse-search" id="browse-search" placeholder="Search your collection…"
          value="${esc(browseState.search)}"
          oninput="browseSearch(this.value)">
        ${_browseFilterTabs()}
      </div>
      <div id="browse-results"><div class="state-msg">Loading...</div></div>
    `);
    document.getElementById('browse-search')?.focus();
  }

  browseState._cache = await api.get('/api/series');
  _renderBrowseResults();
}

function _renderBrowseResults() {
  const { filter, search, _cache: all } = browseState;
  if (!all) return;

  const q = search.toLowerCase();
  const filtered = all.filter(s => {
    if (q && !s.title.toLowerCase().includes(q)) return false;
    const missing = s.missing ?? 0;
    const owned   = s.owned   ?? 0;
    const total   = owned + missing;
    if (filter === 'complete') return total > 0 && missing === 0;
    if (filter === 'partial')  return owned > 0 && missing > 0;
    if (filter === 'missing')  return missing > 0;
    return true;
  });

  if (!filtered.length) {
    const empty = all.length === 0
      ? `<div class="empty-state">
           <div class="empty-state-title">Nothing tracked yet</div>
           <div style="margin-top:8px;color:var(--tq);font-size:13px">
             Use <strong>+ Add Series</strong> to link your Komga library to Metron.
           </div>
         </div>`
      : `<div class="state-msg">No series match.</div>`;
    document.getElementById('browse-results').innerHTML = empty;
    return;
  }

  const cards = filtered.map(s => {
    const pub   = s.publisher ? `<div class="series-card-publisher">${esc(s.publisher.toUpperCase())}</div>` : '';
    const total = (s.owned ?? 0) + (s.missing ?? 0);
    const pct   = total ? Math.round((s.owned / total) * 100) : 0;
    const color = s.missing > 0 ? 'var(--amb)' : (total > 0 ? 'var(--grn)' : 'var(--tq)');
    const nextRelease = s.next_release
      ? `<div class="series-card-next-release">${_fmtReleaseDate(s.next_release)}</div>` : '';
    return `
      <div class="series-card" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="/api/series/${s.id}/thumbnail" alt="${esc(s.title)}"
            onerror="this.style.opacity='0.15'">
          ${nextRelease}
        </div>
        <div class="series-card-bar-track">
          <div class="series-card-bar-fill" style="width:${pct}%;background:${color}"></div>
        </div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(s.title)}</div>
          <div class="series-card-count" style="color:${color}">${s.owned}/${total}</div>
        </div>
        ${pub}
      </div>
    `;
  }).join('');

  document.getElementById('browse-results').innerHTML = `<div class="series-grid">${cards}</div>`;
}

function browseSearch(val) {
  clearTimeout(browseState.searchTimer);
  browseState.searchTimer = setTimeout(() => {
    browseState.search = val;
    _renderBrowseResults();
  }, 300);
}

// --- Metron Match Modal ---

let metronMatchState = { komgaSeries: null, metronId: null };
let metronMatchTimer = null;

function showMetronMatch(jsonStr) {
  const s = JSON.parse(jsonStr);
  const fromMatchReview = metronMatchState.fromMatchReview || false;
  metronMatchState = { komgaSeries: s, metronId: null, fromMatchReview };

  showModal(`
    <div class="modal-title">Track Series</div>
    <div class="match-komga-row">
      <img class="match-thumb" src="/api/komga/series/${esc(s.id)}/thumbnail" alt="" onerror="this.style.opacity='0.2'">
      <div>
        <div class="match-komga-title">${esc(s.name)}</div>
        ${s.publisher ? `<div class="match-komga-meta">${esc(s.publisher.toUpperCase())}${s.year ? ' · ' + s.year : ''}</div>` : ''}
      </div>
    </div>
    <div class="step-label" style="margin-top:20px">Match on Metron</div>
    <input class="search-input" id="metron-match-q" placeholder="Search Metron…"
      value="${esc(s.name)}" oninput="metronMatchSearch(this.value)">
    <div class="search-results" id="metron-match-results"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="match-add-btn" disabled onclick="submitMetronMatch()">Track Series</button>
    </div>
  `);

  metronMatchSearch(s.name);
}

function metronMatchSearch(q) {
  clearTimeout(metronMatchTimer);
  const el = document.getElementById('metron-match-results');
  if (!el || q.length < 2) { if (el) el.innerHTML = ''; return; }
  metronMatchTimer = setTimeout(async () => {
    el.innerHTML = '<div class="state-msg" style="padding:16px 0;font-size:10px">Searching…</div>';
    try {
      const results = await api.get(`/api/search/metron?q=${encodeURIComponent(q)}`);
      if (!document.getElementById('metron-match-results')) return;
      document.getElementById('metron-match-results').innerHTML = results.slice(0, 8).map(r => `
        <div class="search-result" onclick="selectMetronMatch(this, ${r.id})">
          <div>
            <div class="search-result-title">${esc(r.name || r.series_name || '')}</div>
            <div class="search-result-meta">${esc(r.publisher?.name || '')}${r.year_began ? ' · ' + r.year_began : ''}</div>
          </div>
        </div>
      `).join('') || '<div class="state-msg" style="padding:16px 0;font-size:10px">No results.</div>';
    } catch {
      document.getElementById('metron-match-results').innerHTML = '<div class="state-msg" style="padding:16px 0;font-size:10px;color:var(--amb)">Search failed.</div>';
    }
  }, 280);
}

function selectMetronMatch(el, id) {
  metronMatchState.metronId = id;
  document.querySelectorAll('#metron-match-results .search-result').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('match-add-btn').disabled = false;
}

async function submitMetronMatch() {
  const { komgaSeries, metronId, fromMatchReview } = metronMatchState;
  if (!komgaSeries || !metronId) return;
  const btn = document.getElementById('match-add-btn');
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    if (fromMatchReview) {
      await api.post('/api/match/confirm', { komga_series_id: komgaSeries.id, metron_id: metronId });
      closeModal();
      await _refreshMatchReview();
    } else {
      const added = await api.post('/api/series', { komga_id: komgaSeries.id, metron_id: metronId });
      closeModal();
      navigate('series-detail', { id: added.id });
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Track Series';
    console.error(e);
  }
}

// --- Series Detail ---

async function renderSeriesDetail(id) {
  // Untracked series — show preview with Track CTA
  if (!id && currentParams.komgaId) {
    return renderKomgaPreview(currentParams.komgaData);
  }

  setTopbar(`<button class="btn btn-ghost btn-sm" onclick="navigate('library')">← Library</button>`);
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
    } else if (issue.metron_image) {
      inner = `<div class="issue-tile-img ${st}">
        <img src="${esc(issue.metron_image)}" alt="${num}" loading="lazy"
          onerror="this.parentElement.innerHTML=''">
      </div>`;
    } else {
      inner = `<div class="issue-tile-img ${st}"></div>`;
    }
    const dateLabel = (st === 'upcoming' || st === 'missing') && issue.store_date
      ? `<div class="issue-tile-date">${issue.store_date}</div>`
      : '';
    return `<div class="issue-tile" title="${esc(s.title)} ${num}">
      ${inner}
      <div class="issue-tile-num">${num}</div>
      ${dateLabel}
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

async function renderKomgaPreview(s) {
  setTopbar(`<button class="btn btn-ghost btn-sm" onclick="navigate('library')">← Library</button>`);
  const meta = [s.publisher ? s.publisher.toUpperCase() : '', s.year].filter(Boolean).join('  •  ');

  setApp(`
    <div class="detail-band">
      <img class="detail-thumb" src="/api/komga/series/${esc(s.id)}/thumbnail" alt="${esc(s.name)}"
        onerror="this.style.opacity='0.2'">
      <div class="detail-info">
        <div class="detail-title">${esc(s.name)}</div>
        ${meta ? `<div class="detail-meta">${esc(meta)}</div>` : ''}
        <div class="detail-stats-line" style="color:var(--tq)">Loading…</div>
      </div>
    </div>
    <div class="detail-actions-row">
      <button class="btn btn-primary btn-sm"
        data-series="${esc(JSON.stringify(s))}"
        onclick="showMetronMatch(this.dataset.series)">+ Track on Metron</button>
    </div>
    <div class="issue-tabs">
      <div class="issue-tab active">In Library</div>
    </div>
    <div class="issue-grid" id="komga-preview-grid"><div class="state-msg" style="grid-column:1/-1">Loading…</div></div>
  `);

  const books = await api.get(`/api/komga/series/${s.id}/books`);

  const statsLine = `${books.length} book${books.length !== 1 ? 's' : ''} in your library`;
  document.querySelector('.detail-stats-line').textContent = statsLine;

  const grid = document.getElementById('komga-preview-grid');
  if (!grid) return;

  if (!books.length) {
    grid.innerHTML = '<div class="state-msg" style="grid-column:1/-1">No books found in Komga.</div>';
    return;
  }

  grid.innerHTML = books.map(b => {
    const num = b.number_display != null ? `#${fmtNum(b.number_display)}` : '?';
    return `<div class="issue-tile">
      <div class="issue-tile-img">
        <img src="/api/book/${esc(b.id)}/thumbnail" alt="${num}" loading="lazy"
          onerror="this.parentElement.classList.add('unknown');this.remove()">
      </div>
      <div class="issue-tile-num">${num}</div>
    </div>`;
  }).join('');
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
  navigate('library');
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
      <div class="activity-row ${evClass}" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
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

// --- Match Review ---

let _matchPollTimer = null;
let _matchPollGen  = 0;   // incremented each time we (re)enter the page

async function renderMatchReview() {
  document.getElementById('topbar-title').textContent = 'Match Library';
  document.getElementById('topbar-actions').innerHTML =
    `<button class="btn btn-primary btn-sm" id="scan-new-btn" onclick="startScan(this)">Scan New Series</button>`;
  setApp('<div class="state-msg">Loading...</div>');
  clearTimeout(_matchPollTimer);
  _matchPollGen++;
  await _refreshMatchReview(_matchPollGen);
}

const CONF_LABEL = { high: 'Auto', medium: 'Maybe', low: 'Weak', none: 'No match' };
const CONF_COLOR = { high: 'var(--grn)', medium: 'var(--amb)', low: 'var(--amb)', none: 'var(--tq)' };

function _scanFeedRow(r) {
  const conf = r.confidence;
  const label = CONF_LABEL[conf] || 'No match';
  const color = CONF_COLOR[conf] || 'var(--tq)';
  const matchText = r.match ? esc(r.match) : 'No match';
  const candidates = r.candidates || [];
  const safeId = CSS.escape(r.komga_id);

  const pickerRows = candidates.map((c, i) => `
    <label class="scan-candidate-row" onclick="event.stopPropagation()">
      <input type="radio" name="smc_${safeId}" value="${c.id}" ${i === 0 ? 'checked' : ''}>
      <img class="scan-candidate-thumb" src="/api/metron/series/${c.id}/thumbnail" alt=""
        onerror="this.style.opacity='0.15'">
      <span class="scan-candidate-name">${esc(c.name)}</span>
      <span class="scan-candidate-year">${c.year || ''}</span>
      <span class="scan-candidate-score">${Math.round(c.score * 100)}%</span>
    </label>
  `).join('');

  const detail = `
    <div class="scan-feed-detail" id="sfd_${safeId}" hidden>
      <div class="scan-feed-covers">
        <div class="scan-cover-yours">
          <img src="/api/komga/series/${esc(r.komga_id)}/thumbnail" alt=""
            onerror="this.style.opacity='0.15'">
          <span>Yours</span>
        </div>
        ${candidates.length ? `<div class="scan-candidates-wrap"><div class="scan-candidate-list">${pickerRows}</div></div>` : ''}
      </div>
      <div class="scan-feed-actions" onclick="event.stopPropagation()">
        ${candidates.length ? `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); confirmScanRow('${esc(r.komga_id)}', this)">Confirm</button>` : ''}
        <button class="btn btn-ghost btn-sm"
          onclick="event.stopPropagation(); openManualMatch('${esc(r.komga_id)}','${esc(r.title)}')">Search Metron</button>
        <button class="btn btn-ghost btn-sm" style="margin-left:auto;opacity:0.5"
          onclick="event.stopPropagation(); rejectScanRow('${esc(r.komga_id)}', this)">Skip</button>
      </div>
    </div>
  `;

  return `
    <div class="scan-feed-row" id="sfr_${safeId}"
      onclick="toggleScanRow('${esc(r.komga_id)}')" role="button" tabindex="0"
      onkeydown="if(event.key==='Enter')toggleScanRow('${esc(r.komga_id)}')">
      <img class="scan-feed-thumb" src="/api/komga/series/${esc(r.komga_id)}/thumbnail" alt=""
        onerror="this.style.opacity='0.15'">
      <div class="scan-feed-title">${esc(r.title)}</div>
      <div class="scan-feed-arrow">→</div>
      <div class="scan-feed-match" style="color:${color}">${matchText}</div>
      <div class="scan-feed-conf" style="color:${color}">${label}</div>
      <div class="scan-feed-chevron">›</div>
      ${detail}
    </div>
  `;
}

function _scanProgressHtml(status) {
  const pct = status.total ? Math.round((status.done / status.total) * 100) : 0;
  const feed = (status.recent || []).map(_scanFeedRow).join('');
  const label = status.running
    ? `Scanning ${status.done} / ${status.total || '?'} series…`
    : `Scanned ${status.done} series`;
  return `
    <div class="match-progress-wrap">
      <div class="match-progress-bar" style="width:${pct}%"></div>
    </div>
    <div class="match-progress-label">${label}</div>
    <div class="scan-feed" id="scan-feed">${feed}</div>
  `;
}

function toggleScanRow(komgaId) {
  const detail = document.getElementById(`sfd_${CSS.escape(komgaId)}`);
  const row    = document.getElementById(`sfr_${CSS.escape(komgaId)}`);
  if (!detail) return;
  const open = !detail.hidden;
  detail.hidden = open;
  row?.classList.toggle('expanded', !open);
}

async function confirmScanRow(komgaId, btn) {
  const radio = document.querySelector(`input[name="smc_${CSS.escape(komgaId)}"]:checked`);
  if (!radio) return;
  btn.disabled = true; btn.textContent = '…';
  await api.post('/api/match/confirm', { komga_series_id: komgaId, metron_id: parseInt(radio.value) });
  const row = document.getElementById(`sfr_${CSS.escape(komgaId)}`);
  if (row) { row.style.opacity = '0.35'; row.onclick = null; }
}

async function rejectScanRow(komgaId, btn) {
  btn.disabled = true;
  await api.post('/api/match/reject', { komga_series_id: komgaId });
  const row = document.getElementById(`sfr_${CSS.escape(komgaId)}`);
  if (row) row.remove();
}

async function _refreshMatchReview(gen) {
  // Bail if a newer renderMatchReview() call has taken over
  if (gen !== _matchPollGen) return;

  const status = await api.get('/api/match/status');
  if (gen !== _matchPollGen) return;

  if (status.running) {
    const scanBtn = document.getElementById('scan-new-btn');
    if (scanBtn) { scanBtn.disabled = true; scanBtn.textContent = 'Scanning…'; }
    const progressEl = document.getElementById('match-progress');
    if (!progressEl) {
      setApp(`<div id="match-progress">${_scanProgressHtml(status)}</div>`);
    } else {
      const pct = status.total ? Math.round((status.done / status.total) * 100) : 0;
      const barEl = progressEl.querySelector('.match-progress-bar');
      const lblEl = progressEl.querySelector('.match-progress-label');
      if (barEl) barEl.style.width = pct + '%';
      if (lblEl) lblEl.textContent = `Scanning ${status.done} / ${status.total || '?'} series…`;

      const feed = document.getElementById('scan-feed');
      if (feed) {
        for (const r of (status.recent || [])) {
          const existing = document.getElementById(`sfr_${CSS.escape(r.komga_id)}`);
          if (!existing) {
            feed.insertAdjacentHTML('afterbegin', _scanFeedRow(r));
          }
        }
      }
    }
    _matchPollTimer = setTimeout(() => _refreshMatchReview(gen), 1200);
    return;
  }

  clearTimeout(_matchPollTimer);
  const scanBtn = document.getElementById('scan-new-btn');
  if (scanBtn) { scanBtn.disabled = false; scanBtn.textContent = 'Scan New Series'; }

  const { counts } = status;
  const total = counts.high + counts.medium + counts.low + counts.none;

  if (total === 0) {
    setApp(`
      <div class="empty-state">
        <div class="empty-state-title">Nothing to match</div>
        <div style="margin-top:8px;color:var(--tq);font-size:13px">Hit <strong>Scan New Series</strong> to compare your library against Metron.</div>
      </div>
    `);
    return;
  }

  const groups = await api.get('/api/match/candidates');

  const autoConfirmed = status.auto_confirmed || 0;
  const summaryBar = `
    <div class="match-summary-bar">
      ${autoConfirmed ? `
      <div class="match-summary-stat">
        <span class="match-count match-high">${autoConfirmed}</span>
        <span class="match-label">Auto-added</span>
      </div>` : ''}
      <div class="match-summary-stat">
        <span class="match-count match-medium">${counts.medium + counts.low}</span>
        <span class="match-label">Needs review</span>
      </div>
      <div class="match-summary-stat">
        <span class="match-count match-none">${counts.none}</span>
        <span class="match-label">No match</span>
      </div>
    </div>
  `;

  let html = summaryBar;

  // High confidence (any still pending — e.g. from a previous scan before this feature)
  if (groups.high.length) {
    const cards = groups.high.map(c => _matchCard(c, true)).join('');
    html += `
      <div class="match-section">
        <div class="match-section-header">
          <span class="match-section-title">Auto-matched <span class="match-section-count">${groups.high.length}</span></span>
          <button class="btn btn-primary btn-sm" onclick="confirmAllHigh()">Add All</button>
        </div>
        <div class="match-grid" id="match-grid-high">${cards}</div>
      </div>
    `;
  }

  // Medium + low (needs review)
  const review = [...groups.medium, ...groups.low];
  if (review.length) {
    const cards = review.map(c => _matchCard(c, false)).join('');
    html += `
      <div class="match-section">
        <div class="match-section-header">
          <span class="match-section-title">Needs review <span class="match-section-count">${review.length}</span></span>
        </div>
        <div class="match-grid">${cards}</div>
      </div>
    `;
  }

  // No match
  if (groups.none.length) {
    const rows = groups.none.map(c => `
      <div class="match-none-row">
        <img class="match-none-thumb" src="/api/komga/series/${esc(c.komga_series_id)}/thumbnail" alt=""
          onerror="this.style.opacity='0.2'">
        <div class="match-none-title">${esc(c.komga_title)}</div>
        <div class="match-none-meta">${esc(c.komga_publisher || '')}${c.komga_year ? ' · ' + c.komga_year : ''}</div>
        <div class="match-none-actions">
          <button class="btn btn-ghost btn-sm" onclick="openManualMatch('${esc(c.komga_series_id)}', '${esc(c.komga_title)}')">Search Metron</button>
          <button class="btn btn-ghost btn-sm" onclick="rejectCandidate('${esc(c.komga_series_id)}', this)">Skip</button>
        </div>
      </div>
    `).join('');
    html += `
      <div class="match-section">
        <div class="match-section-header">
          <span class="match-section-title">No match found <span class="match-section-count">${groups.none.length}</span></span>
        </div>
        <div class="match-none-list">${rows}</div>
      </div>
    `;
  }

  setApp(html);

  // Build publisher pills from rendered data (populated by _matchCard calls above)
  const pubs = [...new Set(
    Object.values(_matchCardData).map(c => c.komga_publisher).filter(Boolean)
  )].sort();
  const pubPills = pubs.map(p => `
    <button class="match-filter-pub" data-pub="${esc(p.toLowerCase())}"
      onclick="setMatchPubFilter('${esc(p.toLowerCase())}', this)">${esc(p)}</button>
  `).join('');

  const confPills = [
    { val: 'conf-high',   label: 'High' },
    { val: 'conf-medium', label: 'Needs Review' },
    { val: 'conf-low',    label: 'Weak' },
    { val: 'conf-none',   label: 'No Match' },
  ].map(f => `
    <button class="match-filter-conf ${f.val}" data-conf="${f.val}"
      onclick="setMatchConfFilter('${f.val}', this)">${f.label}</button>
  `).join('');

  const filterBar = `
    <div class="match-filter-bar">
      <div class="match-filter-row">
        <input id="match-filter-text" class="match-filter-input" placeholder="Filter by title…"
          oninput="filterMatchCards()">
        <div class="match-filter-confs">${confPills}</div>
      </div>
      ${pubPills ? `<div class="match-filter-pubs">${pubPills}</div>` : ''}
    </div>
  `;
  document.getElementById('app').insertAdjacentHTML('afterbegin', filterBar);
}

let _matchFilterPub  = '';
let _matchFilterConf = '';

function filterMatchCards() {
  const text = (document.getElementById('match-filter-text')?.value || '').toLowerCase();
  const pub  = _matchFilterPub;
  const conf = _matchFilterConf;

  document.querySelectorAll('.match-card').forEach(card => {
    const ok = (!text || card.dataset.title?.includes(text))
            && (!pub  || card.dataset.pub  === pub)
            && (!conf || card.dataset.conf === conf);
    card.style.display = ok ? '' : 'none';
  });

  document.querySelectorAll('.match-none-row').forEach(row => {
    const titleOk = !text || (row.querySelector('.match-none-title')?.textContent || '').toLowerCase().includes(text);
    const pubOk   = !pub  || (row.querySelector('.match-none-meta')?.textContent || '').toLowerCase().startsWith(pub);
    const confOk  = !conf || conf === 'conf-none';
    row.style.display = titleOk && pubOk && confOk ? '' : 'none';
  });

  document.querySelectorAll('.match-section').forEach(section => {
    const hasVisible = [...section.querySelectorAll('.match-card, .match-none-row')]
      .some(el => el.style.display !== 'none');
    section.style.display = hasVisible ? '' : 'none';
  });
}

function setMatchPubFilter(pub, btn) {
  _matchFilterPub = _matchFilterPub === pub ? '' : pub;
  document.querySelectorAll('.match-filter-pub').forEach(el => el.classList.remove('active'));
  if (_matchFilterPub) btn.classList.add('active');
  filterMatchCards();
}

function setMatchConfFilter(conf, btn) {
  _matchFilterConf = _matchFilterConf === conf ? '' : conf;
  document.querySelectorAll('.match-filter-conf').forEach(el => el.classList.remove('active'));
  if (_matchFilterConf) btn.classList.add('active');
  filterMatchCards();
}

// Keyed store so we can pass rich data to the modal without escaping JSON in onclick attrs
const _matchCardData = {};

function _matchCard(c, autoChecked) {
  _matchCardData[c.komga_series_id] = c;
  const badge = _confBadge(c.score);
  const matchLine = c.metron_title
    ? `${esc(c.metron_title)}${c.metron_year ? ' · ' + c.metron_year : ''}`
    : 'No match';

  return `
    <div class="match-card" id="mc_${esc(c.komga_series_id)}"
      data-pub="${esc((c.komga_publisher || '').toLowerCase())}"
      data-title="${esc(c.komga_title.toLowerCase())}"
      data-conf="${badge.cls}"
      onclick="openMatchModal('${esc(c.komga_series_id)}')" role="button" tabindex="0"
      onkeydown="if(event.key==='Enter')openMatchModal('${esc(c.komga_series_id)}')">
      <div class="match-card-img-wrap">
        <img src="/api/komga/series/${esc(c.komga_series_id)}/thumbnail" alt="${esc(c.komga_title)}"
          onerror="this.style.opacity='0.2'">
      </div>
      <div class="match-card-body">
        <div class="match-card-title">${esc(c.komga_title)}</div>
        <div class="match-card-meta">${esc(c.komga_publisher || '')}${c.komga_year ? ' · ' + c.komga_year : ''}</div>
        <div class="match-card-conf-row">
          <span class="match-conf-badge ${badge.cls}">${badge.label}</span>
        </div>
        <div class="match-card-match-line">${matchLine}</div>
        ${autoChecked ? `
          <div class="match-card-actions">
            <button class="btn btn-primary btn-sm"
              onclick="event.stopPropagation(); confirmCard('${esc(c.komga_series_id)}', this, ${c.metron_id})">
              Confirm
            </button>
          </div>` : ''}
      </div>
    </div>
  `;
}

// --- Match detail modal ---

let _modalKomgaId = null;

function _confBadge(score) {
  if (score >= 0.75) return { cls: 'conf-high',   label: 'High confidence' };
  if (score >= 0.45) return { cls: 'conf-medium', label: 'Needs review' };
  if (score >  0.15) return { cls: 'conf-low',    label: 'Weak match' };
  return                    { cls: 'conf-none',   label: 'No match' };
}

function _metronCoverHtml(id, elId) {
  return `
    <div class="match-modal-cover-img-wrap" id="${elId ? elId + '-wrap' : ''}">
      <img id="${elId || ''}" src="/api/metron/series/${id}/thumbnail" alt=""
        onerror="this.parentNode.dataset.err='1'">
      <div class="match-modal-cover-fallback">No cover</div>
    </div>`;
}

function _seriesTypeBadge(name) {
  if (!name) return '';
  const n = name.toUpperCase();
  if (/\bTPB\b/.test(n))      return 'TPB';
  if (/\bHC\b/.test(n) || /HARDCOVER/.test(n)) return 'HC';
  if (/\bOMNIBUS\b/.test(n))  return 'Omnibus';
  if (/\bANNUAL\b/.test(n))   return 'Annual';
  if (/\bSPECIAL\b/.test(n))  return 'Special';
  if (/\bGN\b/.test(n) || /GRAPHIC NOVEL/.test(n)) return 'GN';
  return '';
}

function openMatchModal(komgaId) {
  const c = _matchCardData[komgaId];
  if (!c) return;
  _modalKomgaId = komgaId;
  const candidates = c.candidates || [];
  const first = candidates[0] || { id: c.metron_id, name: c.metron_title, publisher: c.metron_publisher, year: c.metron_year, score: c.score };
  const badge = _confBadge(first.score || c.score || 0);

  const candidateRows = candidates.length ? candidates.map((r, i) => {
    const typeBadge = _seriesTypeBadge(r.name);
    const issueStr  = r.issue_count != null ? `${r.issue_count} issue${r.issue_count === 1 ? '' : 's'}` : '';
    const volStr    = r.volume > 1 ? `Vol. ${r.volume}` : '';
    const metaLine  = [esc(r.publisher || ''), r.year, volStr].filter(Boolean).join(' · ');
    return `
    <label class="match-modal-candidate" data-mid="${r.id}" onclick="event.stopPropagation()">
      <input type="radio" name="mmd_cand" value="${r.id}" ${i === 0 ? 'checked' : ''}
        onchange="updateMatchPreview(this)">
      <div class="match-modal-cand-img-wrap">
        <img class="match-modal-cand-thumb" src="/api/metron/series/${r.id}/thumbnail" alt=""
          onerror="this.parentNode.dataset.err='1'">
        <div class="match-modal-cand-fallback"></div>
      </div>
      <div class="match-modal-cand-info">
        <div class="match-modal-cand-name">${esc(r.name)}</div>
        <div class="match-modal-cand-meta">${metaLine}</div>
      </div>
      ${typeBadge ? `<span class="match-cand-type">${typeBadge}</span>` : ''}
      <span class="match-cand-issues" data-issues="${r.id}">${issueStr}</span>
    </label>`;
  }).join('') : `<div style="color:var(--tq);font-size:12px;padding:8px 0">No candidates found — try Search Metron</div>`;

  const html = `
    <div class="match-modal-body" onclick="event.stopPropagation()">
      <div class="match-modal-covers">
        <div class="match-modal-cover">
          <div class="match-modal-cover-img-wrap">
            <img src="/api/komga/series/${esc(komgaId)}/thumbnail" alt=""
              onerror="this.parentNode.dataset.err='1'">
            <div class="match-modal-cover-fallback">No cover</div>
          </div>
          <div class="match-modal-cover-label">Your library</div>
          <div class="match-modal-cover-sublabel">Komga series thumbnail</div>
          <div class="match-modal-cover-title">${esc(c.komga_title)}</div>
          <div class="match-modal-cover-meta">${esc(c.komga_publisher || '')}${c.komga_year ? ' · ' + c.komga_year : ''}</div>
          <div id="match-owned-count" class="match-modal-cover-stats"></div>
        </div>
        <div class="match-modal-arrow">→</div>
        <div class="match-modal-cover">
          ${_metronCoverHtml(first.id, 'match-preview-img')}
          <div class="match-modal-cover-label">Metron match</div>
          <div class="match-modal-cover-sublabel">Cover from series database</div>
          <div id="match-preview-title" class="match-modal-cover-title">${esc(first.name || '')}</div>
          <div id="match-preview-meta" class="match-modal-cover-meta">${esc(first.publisher || '')}${first.year ? ' · ' + first.year : ''}</div>
          <div id="match-preview-stats" class="match-modal-cover-stats">
            ${_seriesTypeBadge(first.name) ? `<span class="match-cand-type">${_seriesTypeBadge(first.name)}</span>` : ''}
            ${first.issue_count != null ? `<span class="match-preview-issues">${first.issue_count} in series</span>` : ''}
          </div>
        </div>
      </div>
      <div class="match-modal-conf-row">
        <span class="match-conf-badge ${badge.cls}" id="match-preview-badge">${badge.label}</span>
      </div>
      <div class="match-modal-candidates">${candidateRows}</div>
      <div class="match-modal-actions">
        ${candidates.length || c.metron_id ? `
          <button class="btn btn-primary" onclick="confirmFromModal(this)">Confirm</button>` : ''}
        <button class="btn btn-ghost" onclick="openManualMatch('${esc(komgaId)}','${esc(c.komga_title)}'); closeModal()">Search Metron</button>
        <button class="btn btn-ghost" style="margin-left:auto;opacity:0.5" onclick="rejectFromModal(this)">Skip</button>
      </div>
    </div>
  `;

  const modal = document.getElementById('modal');
  modal.classList.add('modal-wide');
  showModal(html);

  api.get(`/api/komga/series/${encodeURIComponent(komgaId)}/books`).then(books => {
    const el = document.getElementById('match-owned-count');
    if (el) el.innerHTML = `<span class="match-preview-issues">${books.length} books in Komga</span>`;
  }).catch(() => {});

  const needsInfo = candidates.filter(r => r.issue_count == null);
  if (needsInfo.length) _backfillCandidateInfo(komgaId, needsInfo);
}

async function _backfillCandidateInfo(komgaId, candidates) {
  await Promise.all(candidates.map(async r => {
    try {
      const info = await api.get(`/api/metron/series/${r.id}/info`);
      if (info.issue_count == null) return;
      const c = _matchCardData[komgaId];
      if (c) {
        const cached = (c.candidates || []).find(x => x.id === r.id);
        if (cached) {
          cached.issue_count = info.issue_count;
          cached.volume      = info.volume;
        }
      }
      const issueStr = `${info.issue_count} issue${info.issue_count === 1 ? '' : 's'}`;
      const span = document.querySelector(`[data-issues="${r.id}"]`);
      if (span) span.textContent = issueStr;
      const radio = document.querySelector(`#modal input[name="mmd_cand"][value="${r.id}"]`);
      if (radio && radio.checked) {
        const stats = document.getElementById('match-preview-stats');
        if (stats) {
          const type = _seriesTypeBadge(r.name);
          stats.innerHTML = (type ? `<span class="match-cand-type">${type}</span>` : '') +
            `<span class="match-preview-issues">${info.issue_count} in series</span>`;
        }
      }
    } catch { /* silently ignore — non-critical */ }
  }));
}

function updateMatchPreview(radio) {
  const c = _matchCardData[_modalKomgaId];
  if (!c) return;
  const cand = (c.candidates || []).find(x => String(x.id) === radio.value);
  if (!cand) return;

  const wrap = document.getElementById('match-preview-img-wrap');
  if (wrap) {
    delete wrap.dataset.err;
    const img = wrap.querySelector('img');
    if (img) img.src = `/api/metron/series/${cand.id}/thumbnail`;
  }
  const title = document.getElementById('match-preview-title');
  const meta  = document.getElementById('match-preview-meta');
  const badge = document.getElementById('match-preview-badge');
  if (title) title.textContent = cand.name || '';
  if (meta)  meta.textContent  = [cand.publisher, cand.year].filter(Boolean).join(' · ');
  if (badge) {
    const b = _confBadge(cand.score);
    badge.className = `match-conf-badge ${b.cls}`;
    badge.textContent = b.label;
  }
  const stats = document.getElementById('match-preview-stats');
  if (stats) {
    const type = _seriesTypeBadge(cand.name);
    const issues = cand.issue_count != null ? `<span class="match-preview-issues">${cand.issue_count} in series</span>` : '';
    stats.innerHTML = (type ? `<span class="match-cand-type">${type}</span>` : '') + issues;
    if (cand.issue_count == null) _backfillCandidateInfo(_modalKomgaId, [cand]);
  }
}

async function confirmFromModal(btn) {
  const radio = document.querySelector('#modal input[name="mmd_cand"]:checked');
  const metronId = radio ? parseInt(radio.value) : (_matchCardData[_modalKomgaId]?.metron_id);
  if (!metronId || !_modalKomgaId) return;
  const komgaId = _modalKomgaId;
  document.getElementById(`mc_${komgaId}`)?.remove();
  closeModal();
  api.post('/api/match/confirm', { komga_series_id: komgaId, metron_id: metronId }).catch(() => {});
}

async function rejectFromModal(btn) {
  if (!_modalKomgaId) return;
  btn.disabled = true;
  try {
    await api.post('/api/match/reject', { komga_series_id: _modalKomgaId });
    document.getElementById(`mc_${_modalKomgaId}`)?.remove();
    closeModal();
  } catch {
    btn.disabled = false;
  }
}

async function startScan(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  await api.post('/api/match/scan', {});
  clearTimeout(_matchPollTimer);
  _matchPollGen++;
  const gen = _matchPollGen;
  setApp(`<div id="match-progress">${_scanProgressHtml({done: 0, total: 0, recent: []})}</div>`);
  _matchPollTimer = setTimeout(() => _refreshMatchReview(gen), 800);
}

async function confirmCard(komgaId, btn, metronId) {
  // If metronId not provided (review card), read from radio selection
  if (!metronId) {
    const radio = document.querySelector(`input[name="mc_${CSS.escape(komgaId)}"]:checked`);
    if (!radio) return;
    metronId = parseInt(radio.value);
  }
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await api.post('/api/match/confirm', { komga_series_id: komgaId, metron_id: metronId });
    document.getElementById(`mc_${komgaId}`)?.remove();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Confirm';
  }
}

async function confirmAllHigh() {
  const grid = document.getElementById('match-grid-high');
  if (!grid) return;
  const cards = grid.querySelectorAll('.match-card');
  const items = [];
  cards.forEach(card => {
    const id = card.id.replace('mc_', '');
    const metronInput = card.querySelector('input[type=hidden]');
    // metron_id is baked into the confirm button's onclick for high-confidence cards
    const btn = card.querySelector('.btn-primary');
    const match = btn?.getAttribute('onclick')?.match(/confirmCard\('[^']+',\s*this,\s*(\d+)/);
    if (match) items.push({ komga_series_id: id, metron_id: parseInt(match[1]) });
  });
  if (!items.length) return;

  const confirmBtn = document.querySelector('#match-grid-high')?.closest('.match-section')?.querySelector('.btn-primary');
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = `Confirming ${items.length}…`; }

  const result = await api.post('/api/match/confirm-bulk', { items });
  await _refreshMatchReview();
}

async function rejectCandidate(komgaId, btn) {
  btn.disabled = true;
  await api.post('/api/match/reject', { komga_series_id: komgaId });
  document.getElementById(`mc_${komgaId}`)?.remove();
  // also handles none-row which doesn't have mc_ id
  btn.closest('.match-card, .match-none-row')?.remove();
}

function openManualMatch(komgaId, komgaTitle) {
  const fakeKomgaSeries = { id: komgaId, name: komgaTitle };
  metronMatchState = { komgaSeries: fakeKomgaSeries, metronId: null, fromMatchReview: true };
  showMetronMatch(JSON.stringify(fakeKomgaSeries));
}

// --- Settings ---

async function renderSettings() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const cfg = await api.get('/api/config');

  setApp(`
    <div class="page-title">Settings</div>
    <div class="settings-grid">
      <div class="settings-card">
        <div class="settings-card-header">Komga</div>
        <div class="settings-field">
          <div class="settings-field-label">Server URL</div>
          <input class="settings-input" id="s-komga-url" value="${esc(cfg.komga_url)}">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Username</div>
          <input class="settings-input" id="s-komga-user" value="${esc(cfg.komga_user)}">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Password</div>
          <input class="settings-input" id="s-komga-pass" type="password" placeholder="Leave blank to keep current">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Library ID</div>
          <input class="settings-input" id="s-komga-lib" value="${esc(cfg.komga_library_id)}">
        </div>
      </div>
      <div>
        <div class="settings-card">
          <div class="settings-card-header">Metron</div>
          <div class="settings-field">
            <div class="settings-field-label">Username</div>
            <input class="settings-input" id="s-metron-user" value="${esc(cfg.metron_user)}">
          </div>
          <div class="settings-field">
            <div class="settings-field-label">Password</div>
            <input class="settings-input" id="s-metron-pass" type="password" placeholder="Leave blank to keep current">
          </div>
        </div>
        <div class="settings-card" style="margin-top:24px">
          <div class="settings-card-header">Comic Vine</div>
          <div class="settings-field">
            <div class="settings-field-label">API Key ${cfg.cv_configured ? '<span style="color:var(--tq);font-size:11px">● connected</span>' : ''}</div>
            <input class="settings-input" id="s-cv-key" type="password" placeholder="${cfg.cv_configured ? 'Leave blank to keep current' : 'Enter API key'}">
          </div>
          <div style="margin-top:8px">
            <button class="btn btn-ghost" onclick="testCV(this)">Test Connection</button>
          </div>
        </div>
        <div class="settings-card" style="margin-top:24px">
          <div class="settings-card-header">Sync Schedule</div>
          <div class="settings-field">
            <div class="settings-field-label">Hours (24h, comma-separated)</div>
            <input class="settings-input" id="s-sync-hours" value="${esc(cfg.sync_hours)}">
          </div>
        </div>
      </div>
      <div class="settings-footer">
        <button class="btn btn-primary" onclick="saveSettings(this)">Save Settings</button>
      </div>
    </div>
  `);
}

async function saveSettings(btn) {
  btn.disabled = true; btn.textContent = 'Saving…';
  const updates = {
    komga_url:        document.getElementById('s-komga-url').value.trim(),
    komga_user:       document.getElementById('s-komga-user').value.trim(),
    komga_library_id: document.getElementById('s-komga-lib').value.trim(),
    metron_user:      document.getElementById('s-metron-user').value.trim(),
    sync_hours:       document.getElementById('s-sync-hours').value.trim(),
  };
  const pass  = document.getElementById('s-komga-pass').value;
  const mpass = document.getElementById('s-metron-pass').value;
  const cvkey = document.getElementById('s-cv-key').value;
  if (pass)  updates.komga_pass  = pass;
  if (mpass) updates.metron_pass = mpass;
  if (cvkey) updates.cv_api_key  = cvkey;

  try {
    await api.patch('/api/config', updates);
    btn.textContent = 'Saved ✓';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Save Settings'; }, 1500);
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Save Settings';
    alert('Save failed — check console.');
    console.error(e);
  }
}

async function testCV(btn) {
  const key = document.getElementById('s-cv-key').value.trim();
  if (!key) { alert('Enter an API key first.'); return; }
  btn.disabled = true; btn.textContent = 'Testing…';
  try {
    const res = await api.post('/api/test/comicvine', { api_key: key });
    btn.textContent = res.ok ? 'Connected ✓' : `Failed: ${res.error}`;
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Test Connection'; }, 2000);
  } catch {
    btn.textContent = 'Error';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Test Connection'; }, 2000);
  }
}

// --- Modal ---

function showModal(html) {
  const modal = document.getElementById('modal');
  modal.innerHTML = html;
  modal.classList.remove('hidden');
  document.getElementById('modal-backdrop').classList.remove('hidden');
  const first = modal.querySelector('button, input, [tabindex]');
  if (first) setTimeout(() => first.focus(), 30);
}

function closeModal() {
  const modal = document.getElementById('modal');
  modal.classList.add('hidden');
  modal.classList.remove('modal-wide');
  document.getElementById('modal-backdrop').classList.add('hidden');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('modal').classList.contains('hidden')) {
    closeModal();
  }
});

// --- Onboarding ---

let onboardState = {
  komgaOk: false,
  metronOk: false,
  libraries: [],
};

async function renderOnboarding() {
  document.getElementById('sidebar').style.opacity = '0.35';
  document.getElementById('sidebar').style.pointerEvents = 'none';
  setTopbar();
  setApp(`
    <div class="onboard-wrap">
      <div class="onboard-title">Welcome to kometa</div>
      <div class="onboard-subtitle">Connect your Komga library and Metron account to get started.</div>

      <div class="onboard-card" id="onboard-komga">
        <div class="onboard-card-header">Komga</div>
        <div class="settings-field">
          <div class="settings-field-label">Server URL</div>
          <input class="settings-input" id="ob-komga-url" placeholder="http://192.168.1.x:8585"
            oninput="onboardReset('komga')">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Username</div>
          <input class="settings-input" id="ob-komga-user" placeholder="your@email.com"
            oninput="onboardReset('komga')">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Password</div>
          <input class="settings-input" id="ob-komga-pass" type="password"
            oninput="onboardReset('komga')">
        </div>
        <div class="onboard-card-footer">
          <span class="onboard-status" id="ob-komga-status"></span>
          <button class="btn btn-ghost btn-sm" onclick="testKomga(this)">Test Connection</button>
        </div>
        <div id="ob-library-wrap" class="hidden">
          <div class="settings-field" style="margin-top:14px">
            <div class="settings-field-label">Library</div>
            <select class="settings-input" id="ob-library-id">
              <option value="">— select library —</option>
            </select>
          </div>
        </div>
      </div>

      <div class="onboard-card" id="onboard-metron">
        <div class="onboard-card-header">Metron</div>
        <div class="settings-field">
          <div class="settings-field-label">Username</div>
          <input class="settings-input" id="ob-metron-user" placeholder="your username"
            oninput="onboardReset('metron')">
        </div>
        <div class="settings-field">
          <div class="settings-field-label">Password</div>
          <input class="settings-input" id="ob-metron-pass" type="password"
            oninput="onboardReset('metron')">
        </div>
        <div class="onboard-card-footer">
          <span class="onboard-status" id="ob-metron-status"></span>
          <button class="btn btn-ghost btn-sm" onclick="testMetron(this)">Test Connection</button>
        </div>
      </div>

      <div class="onboard-footer">
        <button class="btn btn-primary" id="ob-finish-btn" disabled onclick="finishOnboarding(this)">
          Save &amp; Browse Library →
        </button>
      </div>
    </div>
  `);
}

function onboardReset(which) {
  if (which === 'komga') {
    onboardState.komgaOk = false;
    document.getElementById('ob-komga-status').textContent = '';
    document.getElementById('ob-komga-status').className = 'onboard-status';
    document.getElementById('ob-library-wrap')?.classList.add('hidden');
  } else {
    onboardState.metronOk = false;
    document.getElementById('ob-metron-status').textContent = '';
    document.getElementById('ob-metron-status').className = 'onboard-status';
  }
  document.getElementById('ob-finish-btn').disabled = true;
}

function _onboardCheck() {
  const libId = document.getElementById('ob-library-id')?.value;
  const ready = onboardState.komgaOk && onboardState.metronOk && libId;
  document.getElementById('ob-finish-btn').disabled = !ready;
}

async function testKomga(btn) {
  btn.disabled = true; btn.textContent = 'Testing…';
  const status = document.getElementById('ob-komga-status');
  const url  = document.getElementById('ob-komga-url').value.trim();
  const user = document.getElementById('ob-komga-user').value.trim();
  const pass = document.getElementById('ob-komga-pass').value;
  try {
    const res = await api.post('/api/test/komga', { url, user, password: pass });
    if (res.ok) {
      onboardState.komgaOk = true;
      onboardState.libraries = res.libraries;
      status.textContent = '✓ Connected';
      status.className = 'onboard-status ok';
      const wrap = document.getElementById('ob-library-wrap');
      const sel = document.getElementById('ob-library-id');
      sel.innerHTML = '<option value="">— select library —</option>' +
        res.libraries.map(l => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join('');
      if (res.libraries.length === 1) sel.value = res.libraries[0].id;
      wrap.classList.remove('hidden');
      sel.onchange = _onboardCheck;
      _onboardCheck();
    } else {
      status.textContent = '✗ ' + (res.error || 'Failed');
      status.className = 'onboard-status err';
    }
  } catch (e) {
    status.textContent = '✗ Request failed';
    status.className = 'onboard-status err';
  } finally {
    btn.disabled = false; btn.textContent = 'Test Connection';
  }
}

async function testMetron(btn) {
  btn.disabled = true; btn.textContent = 'Testing…';
  const status = document.getElementById('ob-metron-status');
  const user = document.getElementById('ob-metron-user').value.trim();
  const pass = document.getElementById('ob-metron-pass').value;
  try {
    const res = await api.post('/api/test/metron', { user, password: pass });
    if (res.ok) {
      onboardState.metronOk = true;
      status.textContent = '✓ Connected';
      status.className = 'onboard-status ok';
      _onboardCheck();
    } else {
      status.textContent = '✗ ' + (res.error || 'Failed');
      status.className = 'onboard-status err';
    }
  } catch (e) {
    status.textContent = '✗ Request failed';
    status.className = 'onboard-status err';
  } finally {
    btn.disabled = false; btn.textContent = 'Test Connection';
  }
}

async function finishOnboarding(btn) {
  btn.disabled = true; btn.textContent = 'Saving…';
  const updates = {
    komga_url:        document.getElementById('ob-komga-url').value.trim(),
    komga_user:       document.getElementById('ob-komga-user').value.trim(),
    komga_pass:       document.getElementById('ob-komga-pass').value,
    komga_library_id: document.getElementById('ob-library-id').value,
    metron_user:      document.getElementById('ob-metron-user').value.trim(),
    metron_pass:      document.getElementById('ob-metron-pass').value,
  };
  await api.patch('/api/config', updates);
  document.getElementById('sidebar').style.opacity = '';
  document.getElementById('sidebar').style.pointerEvents = '';
  navigate('library');
}

// --- Boot ---

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => navigate(el.dataset.view));
});

async function boot() {
  const cfg = await api.get('/api/config');
  if (!cfg.komga_url) {
    renderOnboarding();
  } else {
    navigate('library');
  }
}

boot();
