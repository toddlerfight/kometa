// --- API ---

let _appConfig = {};
let _issueModalPollTimer = null;

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

let _toastTimer = null;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `toast-show${type === 'error' ? ' toast-error' : ''}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = 'toast-hidden'; }, 3000);
}

// --- Router ---

let currentView = 'series';
let currentParams = {};
let detailTab = 'all';
let detailSortDesc = true;

function navigate(view, params = {}) {
  currentView = view;
  currentParams = params;
  if (view !== 'series-detail') {
    detailTab = 'all';
    document.getElementById('series-bg').classList.add('hidden');
    document.getElementById('series-bg-img').style.backgroundImage = '';
  }
  const hash = params && Object.keys(params).length
    ? `#${view}?${new URLSearchParams(params).toString()}`
    : `#${view}`;
  history.pushState({ view, params }, '', hash);
  updateNav();
  renderView();
}

window.addEventListener('popstate', () => {
  const { view, params } = _parseHash();
  currentView = view;
  currentParams = params;
  if (view !== 'series-detail') {
    detailTab = 'all';
    document.getElementById('series-bg').classList.add('hidden');
    document.getElementById('series-bg-img').style.backgroundImage = '';
  }
  updateNav();
  renderView();
});

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
    default:              setApp('<div class="state-msg">Not found</div>');
  }
}

// --- Helpers ---

function esc(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function _fmtReleaseDate(dateStr) {
  const today = _localToday();
  if (dateStr === today) return 'TODAY';
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

function fmtNum(n) {
  const f = parseFloat(n);
  return Number.isInteger(f) ? String(f) : String(f);
}

function issueStatus(issue) {
  const today = _localToday();
  if (issue.owned) return 'owned';
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
    <button class="btn btn-primary" onclick="showAddWizard()">+ Add Series</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const series = await api.get('/api/series');

  if (!series.length) {
    setApp(`
      <div class="empty-state">
        <div class="empty-state-title">No series tracked yet</div>
        <div class="empty-state-body">Search for a series to start tracking.</div>
        <button class="btn btn-primary" onclick="showAddWizard()">Add Series</button>
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
  if (btn) { btn.disabled = true; btn.textContent = 'Sync started'; }
  api.post('/api/sync', {});
  if (btn) setTimeout(() => { btn.disabled = false; btn.textContent = 'Sync All'; }, 3000);
}

async function sweepSeries(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    const res = await api.post(`/api/series/${id}/search-missing`, {});
    if (res.queued > 0) {
      navigate('activity');
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Sweep Missing'; }
    }
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Sweep Missing'; }
  }
}

async function syncSeries(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  const before = await api.get(`/api/series/${id}`);
  const preSynced = before.last_synced;
  await api.post(`/api/sync/${id}`, {});
  const deadline = Date.now() + 90_000;
  const poll = setInterval(async () => {
    try {
      const s = await api.get(`/api/series/${id}`);
      if (s.last_synced !== preSynced || Date.now() > deadline) {
        clearInterval(poll);
        if (currentView === 'series-detail' && currentParams.id === id) {
          await renderSeriesDetail(id);
        } else {
          await renderSeries();
        }
        if (btn) { btn.disabled = false; btn.textContent = 'Sync'; }
      }
    } catch {
      clearInterval(poll);
      if (btn) { btn.disabled = false; btn.textContent = 'Sync'; }
    }
  }, 2000);
}

// --- Library Browse ---

let browseState = { search: '', searchTimer: null, filter: 'all', _cache: null, sortKey: 'date', sortDir: { date: 'desc' } };

async function renderLibraryBrowse() {
  document.getElementById('topbar-title').textContent = 'Library';
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-ghost btn-sm" onclick="syncAll(this)">Sync All</button>
    <button class="btn btn-primary btn-sm" onclick="showAddWizard()">+ Add Series</button>
  `;
  browseState.search  = '';
  browseState.filter  = 'monitored';
  browseState._cache  = null;
  browseState.sortKey = 'date';
  browseState.sortDir = { date: 'desc' };
  setApp('<div class="state-msg">Loading...</div>');
  await _loadBrowsePage();
}

const BROWSE_FILTERS = [
  { key: 'monitored', label: 'Monitored' },
  { key: 'upcoming',  label: 'Upcoming' },
  { key: 'missing',   label: 'Missing' },
  { key: 'all',       label: 'All' },
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

function _browseSortControls() {
  return `
    <div class="browse-sort">
      <button class="sort-btn${browseState.sortKey === 'alpha' ? ' active' : ''}" id="sort-alpha"
        onclick="browseSort('alpha')" title="Sort by title">
        <span class="sort-btn-icon sort-icon-alpha">A</span>
      </button>
      <button class="sort-btn${browseState.sortKey === 'date' ? ' active' : ''}" id="sort-date"
        onclick="browseSort('date')" title="Sort by release date">
        <span class="sort-btn-icon">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
            <rect x="1" y="2.5" width="12" height="10.5" rx="1.2"/>
            <line x1="1" y1="6" x2="13" y2="6"/>
            <line x1="4.5" y1="1" x2="4.5" y2="4"/>
            <line x1="9.5" y1="1" x2="9.5" y2="4"/>
          </svg>
        </span>
      </button>
      <div class="sort-arrow-box${_isDefaultSort() ? '' : ' non-default'}" id="sort-arrow">
        ${(browseState.sortDir[browseState.sortKey] ?? 'desc') === 'asc' ? '↑' : '↓'}
      </div>
    </div>`;
}

function _isDefaultSort() {
  return browseState.sortKey === 'date' && (browseState.sortDir.date ?? 'desc') === 'desc';
}

function browseSort(key) {
  if (browseState.sortKey === key) {
    browseState.sortDir[key] = browseState.sortDir[key] === 'asc' ? 'desc' : 'asc';
  } else {
    browseState.sortKey = key;
    browseState.sortDir[key] = browseState.sortDir[key] || 'asc';
  }
  const arrowBox = document.getElementById('sort-arrow');
  if (arrowBox) {
    arrowBox.textContent = browseState.sortDir[browseState.sortKey] === 'asc' ? '↑' : '↓';
    arrowBox.classList.toggle('non-default', !_isDefaultSort());
  }
  document.getElementById('sort-alpha')?.classList.toggle('active', browseState.sortKey === 'alpha');
  document.getElementById('sort-date')?.classList.toggle('active', browseState.sortKey === 'date');
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
        ${_browseSortControls()}
      </div>
      <div id="browse-results"><div class="state-msg">Loading...</div></div>
    `);
    document.getElementById('browse-search')?.focus();
  }

  browseState._cache = await api.get('/api/series');
  _renderBrowseResults();
}

function _renderBrowseResults() {
  const { filter, search, sortKey, sortDir, _cache: all } = browseState;
  if (!all) return;

  const q = search.toLowerCase();
  let filtered = all.filter(s => {
    if (q && !s.title.toLowerCase().includes(q)) return false;
    if (filter === 'monitored') return !!s.on_pull_list;
    if (filter === 'upcoming')  return (s.upcoming ?? 0) > 0;
    if (filter === 'missing')   return (s.missing ?? 0) > 0;
    return true;
  });

  if (sortKey === 'alpha') {
    const dir = sortDir.alpha === 'asc' ? 1 : -1;
    filtered = filtered.slice().sort((a, b) => dir * a.title.localeCompare(b.title));
  } else if (sortKey === 'date') {
    const dir = (sortDir.date ?? 'desc') === 'asc' ? 1 : -1;
    filtered = filtered.slice().sort((a, b) => {
      if (!a.next_release && !b.next_release) return 0;
      if (!a.next_release) return 1;
      if (!b.next_release) return -1;
      return dir * a.next_release.localeCompare(b.next_release);
    });
  }

  if (!filtered.length) {
    const needsFolder = _appConfig && !_appConfig.comics_root_ok;
    const empty = all.length === 0
      ? `<div class="empty-state">
           <div class="empty-state-title">${needsFolder ? 'Set your comics folder' : 'Nothing tracked yet'}</div>
           <div style="margin-top:8px;color:var(--tq);font-size:13px">
             ${needsFolder
               ? 'Kometa needs a place to file comics. <button class="btn btn-primary btn-sm" style="margin-left:6px" onclick="_showComicsRootSetup()">Set folder</button>'
               : 'Use <strong>+ Add Series</strong> to start tracking a series.'}
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
    const thumbSrc  = s.next_release_image || `/api/series/${s.id}/thumbnail`;
    const thumbFall = s.next_release_image  ? `this.src='/api/series/${s.id}/thumbnail'` : `this.style.opacity='0.15'`;
    return `
      <div class="series-card" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="${esc(thumbSrc)}" alt="${esc(s.title)}"
            loading="lazy" onerror="${thumbFall}">
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

// --- Series Detail ---

let _detailSeries = null;

function buildIssueTiles(s) {
  const filtered = s.issues.filter(i => {
    const st = issueStatus(i);
    if (detailTab === 'owned')    return st === 'owned';
    if (detailTab === 'missing')  return st === 'missing';
    if (detailTab === 'upcoming') return st === 'upcoming';
    return true;
  }).sort((a, b) => detailSortDesc ? b.number - a.number : a.number - b.number);

  return filtered.map(issue => {
    const st = issueStatus(issue);
    const num = `#${fmtNum(issue.number)}`;
    const dateBadge = st === 'upcoming' && issue.store_date
      ? `<div class="series-card-next-release">${issue.store_date.replace(/-/g, '/')}</div>`
      : '';
    let inner = '';
    if (st === 'owned') {
      const thumbSrc = issue.komga_book_id
        ? `/api/book/${issue.komga_book_id}/thumbnail`
        : `/api/series/${s.id}/issues/${issue.number}/thumbnail`;
      inner = `<div class="issue-tile-img">
        <img src="${thumbSrc}" alt="${num}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">
      </div>`;
    } else if (issue.metron_image) {
      inner = `<div class="issue-tile-img ${st}">
        <img src="${esc(issue.metron_image)}" alt="${num}" loading="lazy"
          onerror="this.remove()">${dateBadge}
      </div>`;
    } else {
      inner = `<div class="issue-tile-img ${st}">${dateBadge}</div>`;
    }
    const searchBtn = st === 'missing'
      ? `<button class="issue-tile-search" title="Search for this issue"
           onclick="event.stopPropagation(); searchIssue(${s.id}, ${issue.number}, this)">↓</button>`
      : '';
    return `<div class="issue-tile" title="${esc(s.title)} ${num}"
      onclick="showIssueModal(${s.id}, ${issue.number})">
      ${inner}
      <div class="issue-tile-num">${num}</div>
      ${searchBtn}
    </div>`;
  }).join('');
}

function flipIssueSort(id) {
  const btn = document.querySelector('.sort-toggle');
  if (btn) {
    btn.title = detailSortDesc ? 'Newest first' : 'Oldest first';
    btn.textContent = detailSortDesc ? '↓ #' : '↑ #';
  }
  const grid = document.querySelector('.issue-grid');
  if (grid && _detailSeries) {
    const tiles = buildIssueTiles(_detailSeries);
    grid.innerHTML = tiles || '<div class="state-msg" style="grid-column:1/-1">Nothing here.</div>';
  }
}

const _autoSynced = new Set();   // series auto-synced this session — fire once each

async function renderSeriesDetail(id) {
  setTopbar(`<button class="btn btn-ghost btn-sm" onclick="navigate('library')">← Library</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const s = await api.get(`/api/series/${id}`);
  _detailSeries = s;

  // Self-healing: a never-synced or stale (>1h) series refreshes itself on view,
  // in the background — no manual Sync button needed. Fires at most once per
  // series per session so a persistently-failing sync can't loop. A successful
  // sync updates last_synced (no longer stale) and re-renders.
  const _lastMs = s.last_synced ? Date.parse(s.last_synced.replace(' ', 'T') + 'Z') : 0;
  if ((Date.now() - _lastMs) > 3600000 && !_autoSynced.has(id)) {
    _autoSynced.add(id);
    syncSeries(id, null);
  }

  const meta = [s.publisher ? s.publisher.toUpperCase() : '', s.year_began].filter(Boolean).join('  •  ');
  const total = s.owned + s.missing + s.upcoming;
  const released = s.owned + s.missing;

  const chips = [
    released > 0 ? `<span class="chip ${s.owned < released ? 'chip-missing' : 'chip-complete'}">${s.owned}/${released}</span>` : '',
    s.upcoming ? `<span class="chip chip-upcoming">${s.upcoming} upcoming</span>` : '',
  ].filter(Boolean).join('');

  const pullBtn = `<button class="btn btn-sm ${s.on_pull_list ? 'btn-primary' : 'btn-ghost'}"
    onclick="togglePullList(${s.id}, ${!s.on_pull_list})">Pull</button>`;

  const tabs = ['all','owned','missing','upcoming'].map(t => `
    <div class="issue-tab ${detailTab === t ? 'active' : ''}" onclick="setDetailTab('${t}', ${id})">${t}</div>
  `).join('');

  const tiles = buildIssueTiles(s);

  const seriesBg = document.getElementById('series-bg');
  const seriesBgImg = document.getElementById('series-bg-img');
  seriesBgImg.style.backgroundImage = `url('/api/series/${s.id}/thumbnail')`;
  seriesBg.classList.remove('hidden');

  setApp(`
    <div class="detail-hero">
      <div class="detail-hero-gradient"></div>
      <div class="detail-hero-content">
        <div class="detail-hero-text">
          <div class="detail-hero-publisher">${esc(meta)}</div>
          <div class="detail-hero-title">${esc(s.title)}</div>
          <div class="detail-hero-chips">${chips}</div>
        </div>
      </div>
    </div>
    <div class="detail-folder-row" id="folder-row">
      <button class="btn-icon" title="Browse for folder" onclick="browseFolderPath(${s.id})"><svg width="13" height="12" viewBox="0 0 13 12" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M1 2.5h4l1 1.5h6v6.5H1V2.5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg></button>
      <span class="detail-folder-path">${esc(s.folder_path || 'Not set')}</span>
      <button class="btn-icon" title="Edit folder path" onclick="editFolderPath(${s.id}, '${esc(s.folder_path || '')}')">✎</button>
      <div class="detail-folder-actions">
        ${pullBtn}
        ${s.missing > 0 ? `<button class="btn btn-ghost btn-sm" onclick="sweepSeries(${s.id}, this)">Sweep Missing</button>` : ''}
        <button class="btn btn-ghost btn-sm" onclick="confirmDelete(${s.id}, '${esc(s.title)}')">Remove</button>
      </div>
    </div>
    <div class="issue-tabs-row">
      <div class="issue-tabs">${tabs}</div>
      <button class="btn-icon sort-toggle" title="${detailSortDesc ? 'Newest first' : 'Oldest first'}"
        onclick="detailSortDesc=!detailSortDesc;flipIssueSort(${id})">
        ${detailSortDesc ? '↓ #' : '↑ #'}
      </button>
    </div>
    <div class="issue-grid">${tiles || `<div class="state-msg" style="grid-column:1/-1">${s.metron_series_id && total === 0 ? 'Syncing issues…' : 'Nothing here.'}</div>`}</div>
  `);

  if (s.metron_series_id && total === 0) {
    const _pollId = setInterval(async () => {
      if (currentView !== 'series-detail' || currentParams.id !== id) { clearInterval(_pollId); return; }
      const fresh = await api.get(`/api/series/${id}`).catch(() => null);
      if (!fresh) { clearInterval(_pollId); return; }
      const freshTotal = fresh.owned + fresh.missing + fresh.upcoming;
      if (freshTotal > 0) { clearInterval(_pollId); renderSeriesDetail(id); }
    }, 3000);
  }
}

function setDetailTab(tab, id) {
  detailTab = tab;
  renderSeriesDetail(id);
}

async function togglePullList(id, on) {
  await api.patch(`/api/series/${id}/pull-list`, { on_pull_list: on });
  renderSeriesDetail(id);
}


let _fbSeriesId = null;
let _fbPath = null;
let _fbCallback = null;
let _fbScope = 'library';   // 'library' = sandboxed to comics root, 'fs' = whole filesystem

// --- Add Series Wizard ---

let _wizardResults = [];
let _wizardSearchTimer = null;
let _wizardState = { idx: -1, metronId: null, source: 'metron', locgId: null };

function showAddWizard() {
  // Can't track or file a series with nowhere to put it. If the comics folder
  // isn't usable, channel the user to set it first — just-in-time, not a gate.
  if (!_appConfig.comics_root_ok) { _showComicsRootSetup(); return; }
  _wizardResults = [];
  _wizardState = { idx: -1, metronId: null, source: 'metron', locgId: null };
  showModal(`
    <div class="modal-title">Add Series</div>
    <div class="wizard-search-row">
      <input class="search-input" id="wizard-search" placeholder="Search for a series…" autocomplete="off"
        onkeydown="if(event.key==='Enter')wizardSearch()">
      <button class="btn btn-primary" onclick="wizardSearch()">Search</button>
    </div>
    <div class="wizard-results" id="wizard-results">
      <div class="state-msg" style="padding:16px 0;font-size:11px">Search for a series…</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
    </div>
  `);
  setTimeout(() => document.getElementById('wizard-search')?.focus(), 50);
}

function _showComicsRootSetup() {
  showModal(`
    <div class="modal-title">Set your comics folder</div>
    <div style="font-size:12px;color:var(--tq);margin:8px 0 14px;line-height:1.5">
      Kometa needs somewhere to file comics before it can track or download them.
      It's the one thing it needs — everything else is optional.
    </div>
    <div class="settings-field">
      <div class="settings-field-label">Comics library path</div>
      <div style="display:flex;gap:6px;align-items:center">
        <input class="search-input" id="setup-comics-root" value="${esc(_appConfig.comics_root || '')}"
          placeholder="/comics" style="flex:1;margin:0;box-sizing:border-box"
          onkeydown="if(event.key==='Enter')saveComicsRoot(document.getElementById('setup-root-btn'))">
        <button class="btn btn-ghost btn-sm" onclick="browseComicsRoot()">Browse</button>
      </div>
    </div>
    <div id="setup-root-err" style="font-size:11px;color:var(--amb);margin-top:6px;min-height:14px"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="setup-root-btn" onclick="saveComicsRoot(this)">Save &amp; Continue</button>
    </div>
  `);
  setTimeout(() => document.getElementById('setup-comics-root')?.focus(), 50);
}

function browseComicsRoot() {
  // Pick the comics root by browsing the whole filesystem (scope='fs') — it's not
  // inside the comics root yet, so the sandboxed library browse can't reach it.
  _fbScope = 'fs';
  _fbCallback = async (path) => {
    // Selecting a folder IS the choice — commit and continue, no second click.
    try {
      if (await _commitComicsRoot(path)) { showAddWizard(); return; }
    } catch (e) { console.error(e); }
    // not usable (e.g. read-only mount) — fall back to the form with it filled in
    _showComicsRootSetup();
    setTimeout(() => {
      const i = document.getElementById('setup-comics-root');
      if (i) i.value = path;
      const err = document.getElementById('setup-root-err');
      if (err) err.textContent = "That folder isn't writable — pick another or check permissions.";
    }, 20);
  };
  showModal('<div class="modal-title">Select Folder</div><div class="fb-loading">Loading…</div>');
  _fbNav('');
}

// PATCH the comics root and refresh health. Returns true if it's now usable.
async function _commitComicsRoot(path) {
  await api.patch('/api/config', { comics_root: path });
  _appConfig = await api.get('/api/config');
  return _appConfig.comics_root_ok;
}

async function saveComicsRoot(btn) {
  const path = document.getElementById('setup-comics-root').value.trim();
  const err = document.getElementById('setup-root-err');
  if (!path) { err.textContent = 'Enter a path.'; return; }
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    if (await _commitComicsRoot(path)) { showAddWizard(); return; }
    err.textContent = "That path doesn't exist or isn't writable — check the mount/permissions.";
    btn.disabled = false; btn.textContent = 'Save & Continue';
  } catch (e) {
    err.textContent = 'Save failed — check console.';
    btn.disabled = false; btn.textContent = 'Save & Continue';
    console.error(e);
  }
}

function _wizardStatus(text) {
  const el = document.getElementById('wizard-results');
  if (!el) return;
  el.innerHTML = `<div class="state-msg _wiz-status" style="padding:16px 0;font-size:11px">${text}<span class="animated-dots"></span></div>`;
}

function _renderWizardResults(results, q) {
  const container = document.getElementById('wizard-results');
  if (!container) return;
  const ql = q.toLowerCase();
  results.sort((a, b) => {
    const at = (a.series || a.name || '').toLowerCase();
    const bt = (b.series || b.name || '').toLowerCase();
    const aExact = at === ql, bExact = bt === ql;
    const aPrefix = at.startsWith(ql), bPrefix = bt.startsWith(ql);
    if (aExact !== bExact) return aExact ? -1 : 1;
    if (aPrefix !== bPrefix) return aPrefix ? -1 : 1;
    return 0;
  });
  _wizardResults = results.slice(0, 15);

  const paint = () => {
    const el = document.getElementById('wizard-results');
    if (!el) return;
    el.innerHTML = _wizardResults.length
      ? _wizardResults.map((r, i) => `
          <div class="wizard-result wizard-result-enter" style="animation-delay:${i * 80}ms" onclick="wizardPickSeries(${i})">
            <img class="wizard-result-thumb" src="${r.source === 'locg' ? esc(r.cover || '') : `/api/metron/series/${r.id}/thumbnail`}" alt=""
              onerror="this.style.opacity=0" loading="lazy">
            <div class="wizard-result-text">
              <div class="wizard-result-title">${esc(r.series || r.name || '')}${r.source === 'locg' ? ' <span class="locg-badge">LOCG</span>' : ''}</div>
              <div class="wizard-result-meta">${esc(r.publisher?.name || '')}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}</div>
            </div>
          </div>`).join('')
      : '<div class="state-msg" style="padding:16px 0;font-size:11px">No results.</div>';
  };

  const status = container.querySelector('._wiz-status');
  if (status) {
    status.style.transition = 'opacity 0.35s ease';
    status.style.opacity = '0';
    setTimeout(paint, 350);
  } else {
    paint();
  }
}

async function wizardSearch() {
  const q = document.getElementById('wizard-search')?.value?.trim() || '';
  if (!document.getElementById('wizard-results') || !q) return;
  try {
    _wizardStatus('Searching…');
    // Metron is optional — if it's not configured or hiccups, don't let it kill
    // the search. Swallow its error and fall through to LOCG (works key-free).
    // The user just searches "for a series"; which source answers is our problem.
    let metronResults = [];
    try {
      metronResults = await api.get(`/api/search/metron?q=${encodeURIComponent(q)}`);
    } catch (e) {
      console.warn('Metron search unavailable, falling back to LOCG:', e);
    }
    if (!document.getElementById('wizard-results')) return;
    if (metronResults.length) { _renderWizardResults(metronResults, q); return; }

    const locgResults = await api.get(`/api/search/locg?q=${encodeURIComponent(q)}`);
    if (!document.getElementById('wizard-results')) return;
    _renderWizardResults(locgResults, q);
  } catch (err) {
    console.error('wizardSearch error:', err);
    const el = document.getElementById('wizard-results');
    if (el) el.innerHTML = `<div class="state-msg" style="padding:16px 0;font-size:11px;color:var(--amb)">Search failed: ${esc(String(err))}</div>`;
  }
}

function wizardPickSeries(idx) {
  const r = _wizardResults[idx];
  if (!r) return;
  _wizardState = { idx, metronId: r.source === 'metron' ? r.id : null, source: r.source || 'metron', locgId: r.source === 'locg' ? r.id : null };
  document.getElementById('modal').innerHTML = `
    <div class="modal-title">Add Series</div>
    <div class="wizard-series-preview">
      <img class="wizard-result-thumb" src="${r.source === 'locg' ? esc(r.cover || '') : `/api/metron/series/${r.id}/thumbnail`}" alt="" onerror="this.style.opacity=0">
      <div class="wizard-result-text">
        <div class="wizard-result-title">${esc(r.series || r.name || '')}</div>
        <div class="wizard-result-meta">${esc(r.publisher?.name || '')}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}</div>
      </div>
    </div>
    <div class="step-label" style="margin-top:16px;margin-bottom:6px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--tq)">Folder path <span style="color:var(--tq);font-weight:400;text-transform:none">(auto-detected — edit if needed)</span></div>
    <div style="display:flex;gap:6px;align-items:center">
      <input class="search-input" id="wizard-folder" placeholder="Resolving…"
        style="flex:1;margin:0">
      <button class="btn btn-ghost btn-sm" onclick="wizardBrowseFolder()">Browse</button>
    </div>
    <div id="wizard-folder-hint" style="margin-top:6px;font-size:10px;color:var(--tq)">&nbsp;</div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px;cursor:pointer;user-select:none">
      <input type="checkbox" id="wizard-pull" checked style="accent-color:var(--pri);width:14px;height:14px">
      <span style="font-family:'Space Mono',monospace;font-size:10px;color:var(--tp)">Add to Pull List</span>
      <span style="font-size:10px;color:var(--tq)">— queue download of all missing issues now</span>
    </label>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="showAddWizard()">← Back</button>
      <button class="btn btn-primary" id="wizard-add-btn" onclick="wizardConfirm()">Track Series</button>
    </div>
  `;
  _previewFolder(r);
}

async function _previewFolder(r) {
  const pub = r.publisher?.name || '';
  const title = r.series || r.name || '';
  try {
    const res = await api.get(`/api/fs/resolve?publisher=${encodeURIComponent(pub)}&title=${encodeURIComponent(title)}`);
    const input = document.getElementById('wizard-folder');
    const hint = document.getElementById('wizard-folder-hint');
    if (!input) return;
    input.placeholder = res.path;
    if (!input.value) input.value = res.path;   // pre-fill, still editable
    if (hint) {
      hint.textContent = res.exists ? '✓ Existing folder — owned issues will be detected' : 'New folder — will be created on first download';
      hint.style.color = res.exists ? 'var(--grn)' : 'var(--tq)';
    }
  } catch (e) {
    const input = document.getElementById('wizard-folder');
    if (input) input.placeholder = '/comics/Publisher/Series Name';
  }
}

function wizardBrowseFolder() {
  const { idx } = _wizardState;
  _fbScope = 'library';
  _fbCallback = (path) => {
    wizardPickSeries(idx);
    setTimeout(() => {
      const inp = document.getElementById('wizard-folder');
      if (inp) inp.value = path;
    }, 20);
  };
  showModal('<div class="modal-title">Select Folder</div><div class="fb-loading">Loading…</div>');
  _fbNav('');
}

async function wizardConfirm() {
  const { metronId, source, locgId } = _wizardState;
  if (!metronId && !locgId) return;
  const r = _wizardResults[_wizardState.idx];
  const folder = (document.getElementById('wizard-folder')?.value || '').trim() || null;
  const onPullList = document.getElementById('wizard-pull')?.checked ?? true;
  const btn = document.getElementById('wizard-add-btn');
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    const payload = {
      folder_path: folder,
      on_pull_list: onPullList,
    };
    if (source === 'locg') {
      payload.locg_id = locgId;
      payload.title = r.series || r.name || '';
      payload.publisher_name = r.publisher?.name || '';
      payload.year_began = r.year_began || null;
    } else {
      payload.metron_id = metronId;
    }
    const added = await api.post('/api/series', payload);
    closeModal();
    navigate('series-detail', { id: added.id });
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Track Series';
    console.error(e);
  }
}

async function browseFolderPath(seriesId) {
  _fbSeriesId = seriesId;
  showModal('<div class="modal-title">Select Folder</div><div class="fb-loading">Loading…</div>');
  await _fbNav('');
}

async function _fbNav(path) {
  _fbPath = path;
  let data;
  try {
    data = await api.get(`/api/fs/browse?scope=${_fbScope}&path=${encodeURIComponent(path)}`);
  } catch {
    document.getElementById('modal').innerHTML += '<div class="fb-error">Failed to load directory.</div>';
    return;
  }
  _fbPath = data.path;

  const items = data.dirs.length
    ? data.dirs.map(d => `<div class="fb-item" data-path="${esc(data.path + '/' + d)}">${esc(d)}</div>`).join('')
    : '<div class="fb-empty">No subdirectories</div>';

  document.getElementById('modal').innerHTML = `
    <div class="modal-title">Select Folder</div>
    <div class="fb-path">${esc(data.path)}</div>
    <div class="fb-list">${items}</div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      ${data.parent ? `<button class="btn btn-ghost btn-sm" data-up="${esc(data.parent)}" id="fb-up">↑ Up</button>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="_fbMkdir()">＋ New Folder</button>
      <button class="btn btn-primary" onclick="_fbSelect()">Select This Folder</button>
    </div>
  `;

  document.querySelectorAll('.fb-item').forEach(el =>
    el.addEventListener('click', () => _fbNav(el.dataset.path))
  );
  const upBtn = document.getElementById('fb-up');
  if (upBtn) upBtn.addEventListener('click', () => _fbNav(upBtn.dataset.up));
}

async function _fbMkdir() {
  const name = prompt('New folder name:');
  if (!name || !name.trim()) return;
  try {
    const res = await api.post('/api/fs/mkdir', { path: _fbPath, name: name.trim(), scope: _fbScope });
    _fbNav(res.path);   // step into the folder you just made, ready to Select it
  } catch (e) {
    alert('Could not create folder — check the name and permissions.');
    console.error(e);
  }
}

async function _fbSelect() {
  if (_fbCallback) {
    const cb = _fbCallback;
    _fbCallback = null;
    cb(_fbPath);
    return;
  }
  await api.patch(`/api/series/${_fbSeriesId}/folder`, { folder_path: _fbPath });
  closeModal();
  renderSeriesDetail(_fbSeriesId);
}

function editFolderPath(id, current) {
  const row = document.getElementById('folder-row');
  if (!row) return;
  row.innerHTML = `
    <svg width="13" height="12" viewBox="0 0 13 12" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M1 2.5h4l1 1.5h6v6.5H1V2.5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" style="color:var(--tq)"/></svg>
    <input class="detail-folder-input" id="folder-input" value="${esc(current)}" placeholder="/comics/Publisher/Series">
    <div class="detail-folder-actions">
      <button class="btn btn-primary btn-sm" onclick="saveFolderPath(${id})">Save</button>
      <button class="btn btn-ghost btn-sm" onclick="renderSeriesDetail(${id})">Cancel</button>
    </div>
  `;
  document.getElementById('folder-input').focus();
}

async function saveFolderPath(id) {
  const val = (document.getElementById('folder-input')?.value || '').trim();
  await api.patch(`/api/series/${id}/folder`, { folder_path: val || null });
  renderSeriesDetail(id);
}


async function searchAllMissing(id, btn) {
  btn.disabled = true; btn.textContent = 'Queuing…';
  try {
    const res = await api.post(`/api/series/${id}/search-missing`, {});
    btn.textContent = `${res.queued} queued`;
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Search Missing'; }, 2000);
  } catch {
    btn.disabled = false; btn.textContent = 'Search Missing';
  }
}

async function searchIssue(seriesId, issueNumber, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await api.post(`/api/series/${seriesId}/issues/${issueNumber}/search`, {});
    btn.textContent = '✓';
    btn.style.opacity = '0.5';
  } catch {
    btn.textContent = '↓';
    btn.disabled = false;
  }
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

function _pullStatus(e) {
  const today = _localToday();
  if (e.owned) return `<span class="pull-status pull-status-owned">✓</span>`;
  if (e.store_date < today) return `<span class="pull-status pull-status-missing">Missing</span>`;
  if (e.store_date === today) return `<span class="pull-status pull-status-today">Today</span>`;
  return `<span class="pull-status pull-status-upcoming">${fmtDayDate(e.store_date)}</span>`;
}

let _pullShowPast = false;

async function renderPullList() {
  setTopbar(`
    <button class="btn btn-sm ${_pullShowPast ? 'btn-primary' : 'btn-ghost'}"
      onclick="_togglePullPast(this)">Last 4 Weeks</button>
  `);
  setApp('<div class="state-msg">Loading...</div>');
  await _renderPullListContent();
}

async function _togglePullPast(btn) {
  _pullShowPast = !_pullShowPast;
  btn.classList.toggle('btn-primary', _pullShowPast);
  btn.classList.toggle('btn-ghost', !_pullShowPast);
  setApp('<div class="state-msg">Loading...</div>');
  await _renderPullListContent();
}

async function _renderPullListContent() {
  const url = _pullShowPast ? '/api/pull-list?days=180&past=28' : '/api/pull-list?days=180';
  const items = await api.get(url);

  if (!items.length) {
    setApp('<div class="page-title">Pull List</div><div class="state-msg">Nothing on your pull list.</div>');
    return;
  }

  const weekStart = (() => { const d = new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate() - d.getDay()); return d; })();

  const groups = _pullShowPast
    ? { 'Previous Releases': [], 'This Week': [], 'Next Week': [], 'Later': [] }
    : { 'This Week': [], 'Next Week': [], 'Later': [] };

  for (const item of items) {
    const d = new Date(item.store_date + 'T00:00:00');
    if (_pullShowPast && d < weekStart) {
      groups['Previous Releases'].push(item);
    } else {
      groups[pullGroup(item.store_date)].push(item);
    }
  }

  const html = Object.entries(groups)
    .filter(([, entries]) => entries.length > 0)
    .map(([label, entries]) => `
      <div class="pull-group">
        <div class="pull-group-label">${label.toUpperCase()}</div>
        ${entries.map(e => {
          const sid = e.id;
          return `
            <div class="pull-row" tabindex="0" role="button"
              onclick="navigate('series-detail', {id: ${sid}})"
              onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${sid}})">
              <img class="pull-thumb" src="/api/series/${sid}/issues/${e.number}/thumbnail" alt=""
                loading="lazy" onerror="this.src='/api/series/${sid}/thumbnail';this.onerror=null">
              <div class="pull-series">${esc(e.title)}</div>
              <div class="pull-issue">#${fmtNum(e.number)}</div>
              ${_pullStatus(e)}
            </div>
          `;
        }).join('')}
      </div>
    `).join('');

  setApp(`<div class="page-title">Pull List</div>${html}`);
}

// --- Activity / Queue ---

const QUEUE_STATE_LABEL = {
  queued: 'Queued', searching: 'Searching', found: 'Found',
  not_found: 'Not found', downloading: 'Downloading',
  processing: 'Processing', done: 'Done', failed: 'Failed',
};
const QUEUE_STATE_COLOR = {
  queued: 'var(--tq)', searching: 'var(--pri)', found: 'var(--pri)',
  not_found: 'var(--su3)', downloading: 'var(--pri)', processing: 'var(--pri)',
  done: 'var(--grn)', failed: 'var(--red, var(--amb))',
};

let _activityPollTimer = null;

function _fmtBytes(n) {
  if (!n) return '';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

async function renderActivity() {
  clearTimeout(_activityPollTimer);
  setTopbar(`
    <button class="btn btn-ghost btn-sm" onclick="triggerSweep(this)">Sweep Missing</button>
    <button class="btn btn-ghost btn-sm" onclick="forceQueueStart(this)">Start Queue</button>
    <button class="btn btn-ghost btn-sm" onclick="clearHistory(this)">Clear History</button>
  `);
  setApp('<div class="state-msg">Loading...</div>');
  await _refreshActivity();
}

async function _refreshActivity() {
  if (currentView !== 'activity') return;
  const queue = await api.get('/api/queue');
  _buildActivityHtml(queue);
  const hasActive = queue.some(q => ['searching','found','downloading','processing'].includes(q.state));
  if (hasActive) _activityPollTimer = setTimeout(_refreshActivity, 2000);
}

function _actChip(state) {
  const map = {
    queued:      ['chip chip-muted',   'Queued'],
    searching:   ['chip chip-active',  'Searching'],
    found:       ['chip chip-muted',   'Found'],
    downloading: ['chip chip-active',  'Downloading'],
    pending_usenet: ['chip chip-active', 'Usenet'],
    processing:  ['chip chip-muted',   'Processing'],
    done:        ['chip chip-done',    'Done'],
    not_found:   ['chip chip-warn',    'Not Found'],
    failed:      ['chip chip-fail',    'Failed'],
  };
  const [cls, label] = map[state] || ['chip chip-muted', state];
  return `<span class="${cls}">${label}</span>`;
}

function _buildActivityHtml(queue) {
  const inProgress = queue.filter(q => ['queued','searching','found','downloading','pending_usenet','processing'].includes(q.state));
  const completed  = queue.filter(q => ['done','not_found','failed'].includes(q.state));

  if (!queue.length) {
    setApp(`<div class="act-empty">
      <div class="act-empty-icon">◌</div>
      <div class="act-empty-msg">Nothing in the queue</div>
      <button class="btn btn-ghost btn-sm" onclick="triggerSweep(this)">Sweep Missing</button>
    </div>`);
    return;
  }

  let html = '<div class="act-wrap">';

  if (inProgress.length) {
    const cards = inProgress.map(q => {
      const numStr = `#${fmtNum(q.issue_number)}`;
      const thumbSrc = q.tracked_series_id ? `/api/series/${q.tracked_series_id}/issues/${q.issue_number}/thumbnail` : '';
      const thumb = thumbSrc ? `<img src="${thumbSrc}" onerror="this.src='/api/series/${q.tracked_series_id}/thumbnail'">` : '';
      const isDownloading = q.state === 'downloading' || q.state === 'pending_usenet';
      const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
      // Usenet progress is a percentage from SAB (no byte counts); GetComics has bytes.
      const detail = q.state === 'pending_usenet'
        ? ' · Usenet'
        : (q.progress ? ' — ' + _fmtBytes(q.progress.done) + ' / ' + _fmtBytes(q.progress.total) : '');
      const progress = isDownloading ? `
        <div class="act-card-progress">
          <div class="act-progress-track"><div class="act-progress-fill" style="width:${pct}%"></div></div>
          <div class="act-progress-text">${pct}%${detail}</div>
        </div>` : '';
      const errTip = q.error ? ` title="${esc(q.error)}"` : '';
      const nav = q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
      // Queued items can stall on a retry_after backoff (dupe guard). Give the
      // user the wheel: kick a search right now, or yank it from the queue.
      const actions = q.state === 'queued' ? `
            <button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)" title="Search GetComics/Usenet now (skip backoff)">Search now</button>
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from queue">✕</button>` : '';
      return `
        <div class="act-card${isDownloading ? '' : ' compact'}"${errTip}>
          <div class="act-card-cover">${thumb}</div>
          <div class="act-card-body"${nav}>
            <div class="act-card-title">${esc(q.title)}</div>
            <div class="act-card-meta">${q.publisher ? esc(q.publisher) + ' · ' : ''}${numStr}</div>
            ${progress}
          </div>
          <div class="act-card-side">${_actChip(q.state)}${actions}</div>
        </div>`;
    }).join('');
    html += `<div class="act-section">
      <div class="act-section-hdr">In Progress <span class="act-count">${inProgress.length}</span></div>
      ${cards}
    </div>`;
  }

  if (completed.length) {
    const rows = completed.map(q => {
      const numStr = `#${fmtNum(q.issue_number)}`;
      const thumbSrc = q.tracked_series_id ? `/api/series/${q.tracked_series_id}/issues/${q.issue_number}/thumbnail` : '';
      const thumb = thumbSrc ? `<img src="${thumbSrc}" onerror="this.src='/api/series/${q.tracked_series_id}/thumbnail'">` : '';
      const isDone = q.state === 'done';
      const errTip = q.error ? ` title="${esc(q.error)}"` : '';
      const nav = q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
      const retry = !isDone ? `<button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)">Retry</button>` : '';
      return `
        <div class="act-row${isDone ? ' done' : ''}"${errTip}>
          <div class="act-row-cover">${thumb}</div>
          <div class="act-row-meta"${nav}>
            <div class="act-row-title">${esc(q.title)}</div>
            <div class="act-row-issue">${numStr}</div>
          </div>
          <div class="act-row-actions">
            ${_actChip(q.state)}
            ${retry}
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)">✕</button>
          </div>
        </div>`;
    }).join('');
    html += `<div class="act-section">
      <div class="act-section-hdr">Completed <span class="act-count">${completed.length}</span></div>
      ${rows}
    </div>`;
  }

  html += '</div>';
  setApp(html);
}

async function triggerSweep(btn) {
  btn.disabled = true; btn.textContent = 'Sweeping…';
  await api.post('/api/queue/sweep', {});
  btn.textContent = 'Queued ✓';
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Sweep Missing'; renderActivity(); }, 1500);
}

async function forceQueueStart(btn) {
  btn.disabled = true; btn.textContent = 'Starting…';
  await api.post('/api/queue/process', {});
  btn.textContent = 'Started ✓';
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Start Queue'; _refreshActivity(); }, 1000);
}

async function clearHistory(btn) {
  btn.disabled = true; btn.textContent = 'Clearing…';
  await api.post('/api/queue/clear-history', {});
  btn.disabled = false; btn.textContent = 'Clear History';
  _refreshActivity();
}

async function retryQueue(id, btn) {
  btn.disabled = true;
  await api.post(`/api/queue/${id}/retry`, {});
  renderActivity();
}

async function removeQueue(id, btn) {
  btn.disabled = true;
  await api.del(`/api/queue/${id}`);
  renderActivity();
}

// --- Settings ---

async function renderSettings() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const cfg = await api.get('/api/config');

  setApp(`
    <div class="page-title">Settings</div>
    <div class="settings-grid">
      <div>
      <div class="settings-card">
        <div class="settings-card-header">Comics Library <span style="font-weight:400;font-size:11px;color:var(--tq)">(required)</span></div>
        <div class="settings-field">
          <div class="settings-field-label">Library path</div>
          <input class="settings-input" id="s-comics-root" value="${esc(cfg.comics_root || '')}" placeholder="/comics">
        </div>
        <div style="font-size:10px;color:var(--tq);margin-top:2px">Where comics live and get filed. Downloads stage in a hidden subfolder of this path.</div>
      </div>
      <div class="settings-card" style="margin-top:24px">
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
          <div class="settings-card-header">League of Comic Geeks <span style="font-weight:400;font-size:11px;color:var(--tq)">(optional)</span></div>
          <div class="settings-field">
            <div class="settings-field-label">Username ${cfg.locg_configured ? '<span style="color:var(--tq);font-size:11px">● connected</span>' : ''}</div>
            <input class="settings-input" id="s-locg-user" value="${esc(cfg.locg_user || '')}">
          </div>
          <div class="settings-field">
            <div class="settings-field-label">Password</div>
            <input class="settings-input" id="s-locg-pass" type="password" placeholder="${cfg.locg_configured ? 'Leave blank to keep current' : 'Enter password'}">
          </div>
        </div>
        <div class="settings-card" style="margin-top:24px">
          <div class="settings-card-header">Sync Schedule</div>
          <div class="settings-field">
            <div class="settings-field-label">Hours (24h, comma-separated)</div>
            <input class="settings-input" id="s-sync-hours" value="${esc(cfg.sync_hours)}">
          </div>
        </div>
        <div class="settings-card" style="margin-top:24px">
          <div class="settings-card-header">SABnzbd <span style="font-weight:400;font-size:11px;color:var(--tq)">(optional — Usenet downloads)</span></div>
          <div class="settings-field">
            <div class="settings-field-label">Server URL ${cfg.sab_configured ? '<span style="color:var(--tq);font-size:11px">● connected</span>' : ''}</div>
            <input class="settings-input" id="s-sab-url" value="${esc(cfg.sab_url || '')}" placeholder="http://host:8080">
          </div>
          <div class="settings-field">
            <div class="settings-field-label">API Key</div>
            <input class="settings-input" id="s-sab-apikey" type="password" placeholder="${cfg.sab_configured ? 'Leave blank to keep current' : 'Enter API key'}">
          </div>
          <div id="indexers-section"></div>
        </div>
      </div>
      <div class="settings-footer">
        <button class="btn btn-primary" onclick="saveSettings(this)">Save Settings</button>
      </div>
    </div>
  `);
  _renderIndexers(cfg.newznab_indexers);
}

function _renderIndexers(list) {
  const el = document.getElementById('indexers-section');
  if (!el) return;
  const rows = (list || []).map((ix, i) => `
    <div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
      <span style="flex:1">${esc(ix.name)} <span style="color:var(--tq)">${esc(ix.host)}${ix.ssl ? '' : ' · no ssl'}</span></span>
      <button class="btn btn-ghost btn-sm" onclick="removeIndexer(${i})">Remove</button>
    </div>`).join('') || '<div style="color:var(--tq);font-size:11px;padding:3px 0">No indexers yet.</div>';
  el.innerHTML = `
    <div class="settings-field-label" style="margin-top:16px">Newznab Indexers <span style="color:var(--tq);font-weight:400">(saved immediately)</span></div>
    ${rows}
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <input class="settings-input" id="ix-name" placeholder="Name" style="flex:1;min-width:70px">
      <input class="settings-input" id="ix-host" placeholder="api.example.info" style="flex:2;min-width:130px">
      <input class="settings-input" id="ix-apikey" type="password" placeholder="API key" style="flex:1;min-width:70px">
    </div>
    <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
      <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--tp);cursor:pointer"><input type="checkbox" id="ix-ssl" checked> SSL</label>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto" onclick="addIndexer(this)">Add Indexer</button>
    </div>`;
}

async function addIndexer(btn) {
  const name = document.getElementById('ix-name').value.trim();
  const host = document.getElementById('ix-host').value.trim();
  const apikey = document.getElementById('ix-apikey').value.trim();
  const ssl = document.getElementById('ix-ssl').checked;
  if (!name || !host || !apikey) { alert('Name, host, and API key are all required.'); return; }
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    await api.post('/api/config/indexers', { name, host, apikey, ssl });
    const cfg = await api.get('/api/config');
    _renderIndexers(cfg.newznab_indexers);  // re-renders cleared inputs too
  } catch (e) { btn.disabled = false; btn.textContent = 'Add Indexer'; alert('Add failed — check console.'); console.error(e); }
}

async function removeIndexer(idx) {
  try {
    await api.del(`/api/config/indexers/${idx}`);
    const cfg = await api.get('/api/config');
    _renderIndexers(cfg.newznab_indexers);
  } catch (e) { alert('Remove failed — check console.'); console.error(e); }
}

async function saveSettings(btn) {
  btn.disabled = true; btn.textContent = 'Saving…';
  const updates = {
    comics_root:      document.getElementById('s-comics-root').value.trim(),
    komga_url:        document.getElementById('s-komga-url').value.trim(),
    komga_user:       document.getElementById('s-komga-user').value.trim(),
    komga_library_id: document.getElementById('s-komga-lib').value.trim(),
    metron_user:      document.getElementById('s-metron-user').value.trim(),
    locg_user:        document.getElementById('s-locg-user').value.trim(),
    sync_hours:       document.getElementById('s-sync-hours').value.trim(),
    sab_url:          document.getElementById('s-sab-url').value.trim(),
  };
  const pass      = document.getElementById('s-komga-pass').value;
  const mpass     = document.getElementById('s-metron-pass').value;
  const cvkey     = document.getElementById('s-cv-key').value;
  const locgpass  = document.getElementById('s-locg-pass').value;
  const sabkey    = document.getElementById('s-sab-apikey').value;
  if (pass)     updates.komga_pass  = pass;
  if (mpass)    updates.metron_pass = mpass;
  if (cvkey)    updates.cv_api_key  = cvkey;
  if (locgpass) updates.locg_pass   = locgpass;
  if (sabkey)   updates.sab_apikey  = sabkey;

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
  clearTimeout(_issueModalPollTimer);
  const modal = document.getElementById('modal');
  modal.classList.add('hidden');
  modal.classList.remove('modal-wide');
  document.getElementById('modal-backdrop').classList.add('hidden');
}

// --- Issue Detail Modal ---

let _issueVariantCovers  = [];
let _issueVariantSelected = new Set();
let _issueVariantPrimary  = null;
let _issueVariantFetched  = false;
let _issueVariantSeriesId = null;
let _issueVariantNumber   = null;

async function showIssueModal(seriesId, number) {
  clearTimeout(_issueModalPollTimer);
  const issue = _detailSeries?.issues?.find(i => i.number === number);
  if (!issue) return;

  _issueVariantCovers   = [];
  _issueVariantSelected = new Set();
  _issueVariantPrimary  = null;
  _issueVariantFetched  = false;
  _issueVariantSeriesId = seriesId;
  _issueVariantNumber   = number;

  const s = _detailSeries;
  const st = issueStatus(issue);
  const num = `#${fmtNum(number)}`;

  const imgSrc = issue.komga_book_id
    ? `/api/book/${esc(issue.komga_book_id)}/thumbnail`
    : (issue.metron_image || '');

  const chipMap = {
    owned:   `<span class="chip chip-complete">Owned</span>`,
    missing: `<span class="chip chip-missing">Missing</span>`,
    upcoming:`<span class="chip chip-upcoming">Upcoming</span>`,
    unknown: `<span class="chip" style="color:var(--tq);border-color:var(--tq)">Unknown</span>`,
  };

  let dateHtml = '';
  if (issue.store_date) {
    const d = new Date(issue.store_date + 'T00:00:00');
    const fmtDate = d.toLocaleDateString('en', { month: 'long', day: 'numeric', year: 'numeric' });
    if (st === 'upcoming') {
      const daysAway = Math.max(0, Math.round((d - Date.now()) / 86400000));
      dateHtml = `<div class="issue-modal-date">${fmtDate}</div>
                  <div class="issue-modal-days">${daysAway} day${daysAway !== 1 ? 's' : ''} away</div>`;
    } else {
      dateHtml = `<div class="issue-modal-release-label">Released ${fmtDate}</div>`;
    }
  }

  let footerAction = '';
  if (st === 'owned' && issue.komga_book_id && _appConfig.komga_url) {
    const readerUrl = `${_appConfig.komga_url}/book/${esc(issue.komga_book_id)}/read`;
    footerAction = `<a class="btn btn-primary" href="${readerUrl}" target="_blank" rel="noopener">Open in Komga</a>`;
  } else if (st === 'missing') {
    footerAction = `<button class="btn btn-primary" id="issue-dl-btn" onclick="issueDownload(${seriesId}, ${number})">Download</button>`;
  }

  const hasLocgId = !!issue.locg_issue_id;

  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      <div class="issue-modal-cover">
        ${imgSrc
          ? `<img src="${esc(imgSrc)}" alt="${num}" onerror="this.style.opacity='0.1'">`
          : `<div class="issue-modal-no-cover"></div>`}
      </div>
      <div class="issue-modal-info">
        <div class="issue-modal-num">${num}</div>
        <div class="issue-modal-series">${esc(s.title)}</div>
        <div class="issue-modal-meta">${esc([s.publisher, s.year_began].filter(Boolean).join(' · '))}</div>
        <div style="margin:8px 0">${chipMap[st] || ''}</div>
        ${dateHtml}
        ${hasLocgId ? `
        <div class="issue-modal-tabs" style="margin-top:12px">
          <button class="issue-modal-tab active" id="imtab-details"  onclick="_imSwitchTab('details')">Details</button>
          <button class="issue-modal-tab"        id="imtab-variants" onclick="_imSwitchTab('variants')">
            Variants <span class="vtab-badge" id="imtab-variants-badge" style="display:none"></span>
          </button>
        </div>` : ''}
        <div class="issue-modal-panel active" id="impanel-details">
          <div class="issue-modal-details" id="issue-modal-details">
            ${issue.metron_issue_id ? '<div class="state-msg" style="font-size:11px;padding:8px 0">Loading details…</div>' : ''}
          </div>
        </div>
        ${hasLocgId ? `
        <div class="issue-modal-panel" id="impanel-variants">
          <div id="variant-area" class="variant-loading">Loading covers from LOCG…</div>
          <div id="variant-footer" class="variant-footer" style="display:none">
            <div class="variant-hint" id="variant-hint">Select variants to include, ★ one as the cover.</div>
            <button class="btn btn-primary btn-sm" id="variant-apply-btn" disabled
              onclick="_imApplyVariants(${seriesId}, ${number}, ${st === 'owned' ? 'true' : 'false'})">Apply</button>
          </div>
        </div>` : ''}
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${footerAction}
    </div>
  `);

  // Fetch Metron detail async
  if (issue.metron_issue_id) {
    try {
      const detail = await api.get(`/api/series/${seriesId}/issues/${number}/metron`);
      const detailEl = document.getElementById('issue-modal-details');
      if (!detailEl) return;
      let html = '';
      if (detail.desc) {
        html += `<div class="issue-modal-desc">${esc(detail.desc)}</div>`;
      }
      if (detail.credits?.length) {
        const grouped = {};
        for (const c of detail.credits) {
          const role = c.role?.name || 'Other';
          const name = c.creator?.name || '';
          if (name) (grouped[role] = grouped[role] || []).push(name);
        }
        html += '<div class="issue-modal-credits">' +
          Object.entries(grouped).map(([role, names]) =>
            `<div class="issue-modal-credit-row">
              <div class="issue-modal-credit-role">${esc(role)}</div>
              <div class="issue-modal-credit-name">${names.map(esc).join(', ')}</div>
            </div>`
          ).join('') + '</div>';
      }
      detailEl.innerHTML = html || '';
    } catch {
      const el = document.getElementById('issue-modal-details');
      if (el) el.innerHTML = '';
    }
  }

  // Fetch variants in background
  if (hasLocgId) _imFetchVariants(seriesId, number);

  if (st === 'missing') _pollIssueQueue(seriesId, number);
}

function _imSwitchTab(name) {
  ['details', 'variants'].forEach(t => {
    document.getElementById(`imtab-${t}`)?.classList.toggle('active', t === name);
    document.getElementById(`impanel-${t}`)?.classList.toggle('active', t === name);
  });
}

async function _imFetchVariants(seriesId, number) {
  try {
    const data = await api.get(`/api/series/${seriesId}/issues/${number}/variants`);
    if (seriesId !== _issueVariantSeriesId || number !== _issueVariantNumber) return;
    _issueVariantCovers  = data.covers || [];
    _issueVariantFetched = true;
    _imRenderVariants();
    if (_issueVariantCovers.length > 1) {
      const badge = document.getElementById('imtab-variants-badge');
      if (badge) { badge.textContent = _issueVariantCovers.length; badge.style.display = 'inline-flex'; }
    }
  } catch(e) {
    const el = document.getElementById('variant-area');
    if (el) el.innerHTML = `<div class="variant-empty">Could not load variants: ${esc(e.message)}</div>`;
  }
}

function _imRenderVariants() {
  const area = document.getElementById('variant-area');
  if (!area) return;
  if (!_issueVariantCovers.length) {
    area.innerHTML = '<div class="variant-empty">No variants found.</div>';
    return;
  }
  area.className = '';
  area.innerHTML = `<div class="variant-grid">${
    _issueVariantCovers.map((c, i) => `
      <div class="v-card" id="vc-${c.id}" style="animation-delay:${i*25}ms" onclick="_imToggleVariant('${c.id}')">
        <div class="v-cover">
          <img src="${esc(c.thumb)}" alt="${esc(c.name)}" loading="lazy"
            onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
          <div class="no-img" style="display:none">No image</div>
          <div class="v-tick">✓</div>
          <button class="v-star" onclick="_imSetPrimary(event,'${c.id}')" title="Set as cover">★</button>
          <div class="v-primary-label">COVER</div>
        </div>
        <div class="v-name">${esc(c.name)}</div>
      </div>`).join('')
  }</div>`;
  const footer = document.getElementById('variant-footer');
  if (footer) footer.style.display = 'flex';
}

function _imToggleVariant(id) {
  if (_issueVariantSelected.has(id)) {
    _issueVariantSelected.delete(id);
    if (_issueVariantPrimary === id)
      _issueVariantPrimary = _issueVariantSelected.size ? [..._issueVariantSelected][0] : null;
  } else {
    _issueVariantSelected.add(id);
    if (!_issueVariantPrimary) _issueVariantPrimary = id;
  }
  _imRefreshCards();
  _imUpdateHint();
}

function _imSetPrimary(e, id) {
  e.stopPropagation();
  if (!_issueVariantSelected.has(id)) {
    _issueVariantSelected.add(id);
    if (!_issueVariantPrimary) _issueVariantPrimary = id;
  }
  _issueVariantPrimary = id;
  _imRefreshCards();
  _imUpdateHint();
}

function _imRefreshCards() {
  _issueVariantCovers.forEach(c => {
    const el = document.getElementById(`vc-${c.id}`);
    if (!el) return;
    el.classList.toggle('selected',   _issueVariantSelected.has(c.id));
    el.classList.toggle('is-primary', c.id === _issueVariantPrimary);
  });
  const btn = document.getElementById('variant-apply-btn');
  if (btn) btn.disabled = _issueVariantSelected.size === 0;
}

function _imUpdateHint() {
  const hint = document.getElementById('variant-hint');
  if (!hint) return;
  const n = _issueVariantSelected.size;
  if (n === 0) { hint.innerHTML = 'Select variants to include, ★ one as the cover.'; return; }
  const primary = _issueVariantCovers.find(c => c.id === _issueVariantPrimary);
  const pName = primary ? `<strong>${esc(primary.name)}</strong>` : '—';
  hint.innerHTML = `${n} variant${n > 1 ? 's' : ''} · Cover: ${pName}`;
}

async function _imApplyVariants(seriesId, number, isOwned) {
  if (!_issueVariantSelected.size) return;
  const btn = document.getElementById('variant-apply-btn');
  if (btn) { btn.disabled = true; btn.textContent = isOwned ? 'Building…' : 'Saving…'; }
  const selected = _issueVariantCovers.filter(c => _issueVariantSelected.has(c.id));
  try {
    const res = await api.post(`/api/series/${seriesId}/issues/${number}/variants/apply`, {
      selected,
      primary_id: _issueVariantPrimary,
    });
    if (isOwned) {
      showToast(`${res.added} variant cover${res.added > 1 ? 's' : ''} added to CBZ`);
    } else {
      showToast(`${selected.length} variant${selected.length > 1 ? 's' : ''} queued for download`);
    }
    closeModal();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
    showToast(`Error: ${e.message || 'failed'}`, 'error');
  }
}

function _pollIssueQueue(seriesId, number) {
  clearTimeout(_issueModalPollTimer);
  _issueModalPollTimer = setTimeout(async () => {
    if (!document.getElementById('issue-dl-btn')) return;
    try {
      const qs = await api.get(`/api/series/${seriesId}/issues/${number}/queue-status`);
      _updateIssueDlBtn(seriesId, number, qs);
      if (qs.state && !['done', 'failed', 'not_found'].includes(qs.state)) {
        _pollIssueQueue(seriesId, number);
      }
    } catch {}
  }, 2000);
}

function _updateIssueDlBtn(seriesId, number, qs) {
  const btn = document.getElementById('issue-dl-btn');
  if (!btn) return;
  const { state, progress } = qs;
  if (!state)               { btn.disabled = false; btn.textContent = 'Download'; btn.onclick = () => issueDownload(seriesId, number); return; }
  if (state === 'queued')   { btn.disabled = true;  btn.textContent = 'Queued…'; return; }
  if (state === 'searching'){ btn.disabled = true;  btn.textContent = 'Searching…'; return; }
  if (state === 'downloading') {
    const pct = progress?.total ? Math.round(progress.done / progress.total * 100) : 0;
    btn.disabled = true; btn.textContent = `Downloading ${pct}%`;
    return;
  }
  if (state === 'done') {
    btn.disabled = true; btn.textContent = 'Done ✓';
    btn.style.cssText += ';background:var(--grn);border-color:var(--grn)';
    return;
  }
  btn.disabled = false;
  btn.textContent = state === 'not_found' ? 'Not Found · Retry' : 'Failed · Retry';
  btn.onclick = () => issueDownload(seriesId, number);
}

async function issueDownload(seriesId, number) {
  const btn = document.getElementById('issue-dl-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Queuing…'; }
  try {
    await api.post(`/api/series/${seriesId}/issues/${number}/search`, {});
    _pollIssueQueue(seriesId, number);
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Download'; }
  }
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

function _parseHash() {
  const raw = location.hash.slice(1);
  if (!raw) return { view: 'library', params: {} };
  const [view, qs] = raw.split('?');
  const params = qs ? Object.fromEntries(new URLSearchParams(qs)) : {};
  // coerce numeric id back to number
  if (params.id) params.id = parseInt(params.id, 10) || params.id;
  return { view: view || 'library', params };
}

async function boot() {
  // Always land on the library. Komga and Metron are optional integrations,
  // configured in Settings — never a blocking welcome gate. Kometa runs fine
  // with neither: search and track via LOCG, own via folders.
  const cfg = await api.get('/api/config');
  _appConfig = cfg;
  const { view, params } = _parseHash();
  navigate(view, params);
}

boot();
