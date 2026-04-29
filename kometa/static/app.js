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

let browseState = { page: 0, search: '', searchTimer: null, filter: 'all' };

async function renderLibraryBrowse() {
  setTopbar(`<button class="btn btn-ghost" onclick="syncAll(this)">Sync All</button>`);
  setApp('<div class="state-msg">Loading...</div>');
  browseState.page = 0;
  browseState.search = '';
  browseState.filter = 'all';
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
  browseState.page = 0;
  browseState.search = '';
  const input = document.getElementById('browse-search');
  if (input) input.value = '';
  document.querySelectorAll('.browse-filter-tab').forEach(b =>
    b.classList.toggle('active', b.textContent.toLowerCase() === key)
  );
  _loadBrowsePage();
}

async function _loadBrowsePage() {
  const { page, search, filter } = browseState;

  const firstRender = !document.getElementById('browse-search');
  if (firstRender) {
    setApp(`
      <div class="browse-header">
        <div class="page-title" style="margin:0;border:none;padding:0">Library</div>
        <input class="browse-search" id="browse-search" placeholder="Search…"
          value="${esc(search)}"
          oninput="browseSearch(this.value)">
      </div>
      ${_browseFilterTabs()}
      <div id="browse-results"><div class="state-msg">Loading...</div></div>
    `);
    if (filter === 'all') document.getElementById('browse-search')?.focus();
  }

  let cards, pagination;

  if (filter === 'all') {
    const qs = `page=${page}&size=48${search ? '&search=' + encodeURIComponent(search) : ''}`;
    const data = await api.get(`/api/library/komga?${qs}`);

    cards = data.items.map(s => {
      const pub = s.publisher ? `<div class="series-card-publisher">${esc(s.publisher.toUpperCase())}</div>` : '';
      if (s.tracked) {
        return `
          <div class="series-card browse-tracked" tabindex="0" role="button"
            onclick="navigate('series-detail', {id: ${s.tracked_id}})"
            onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.tracked_id}})">
            <div class="series-card-img-wrap">
              <img class="series-card-cover" src="/api/komga/series/${esc(s.id)}/thumbnail" alt="${esc(s.name)}"
                onerror="this.style.opacity='0.15'">
              <div class="browse-tracked-badge">Tracked</div>
            </div>
            <div class="series-card-footer">
              <div class="series-card-title">${esc(s.name)}</div>
            </div>
            ${pub}
          </div>
        `;
      }
      return `
        <div class="series-card browse-card" tabindex="0" role="button"
          data-series="${esc(JSON.stringify(s))}"
          onclick="navigateKomgaSeries(this.dataset.series)"
          onkeydown="if(event.key==='Enter'||event.key===' ')navigateKomgaSeries(this.dataset.series)">
          <div class="series-card-img-wrap">
            <img class="series-card-cover" src="/api/komga/series/${esc(s.id)}/thumbnail" alt="${esc(s.name)}"
              onerror="this.style.opacity='0.15'">
            <div class="browse-add-overlay"><span>View</span></div>
          </div>
          <div class="series-card-footer">
            <div class="series-card-title">${esc(s.name)}</div>
          </div>
          ${pub}
        </div>
      `;
    }).join('');

    const prevBtn = page > 0
      ? `<button class="btn btn-ghost btn-sm" onclick="browsePage(${page - 1})">← Prev</button>`
      : `<button class="btn btn-ghost btn-sm" disabled>← Prev</button>`;
    const nextBtn = !data.last
      ? `<button class="btn btn-ghost btn-sm" onclick="browsePage(${page + 1})">Next →</button>`
      : `<button class="btn btn-ghost btn-sm" disabled>Next →</button>`;
    const showing = `${page * 48 + 1}–${Math.min((page + 1) * 48, data.total)} of ${data.total}`;
    pagination = `<div class="browse-pagination">${prevBtn}<span class="browse-page-info">${showing}</span>${nextBtn}</div>`;

  } else {
    const all = await api.get('/api/series');
    const today = new Date().toISOString().slice(0, 10);
    const filtered = all.filter(s => {
      const missing = s.missing ?? 0;
      const owned   = s.owned   ?? 0;
      const total   = owned + missing;
      if (filter === 'complete') return total > 0 && missing === 0;
      if (filter === 'partial')  return owned > 0 && missing > 0;
      if (filter === 'missing')  return missing > 0;
      return true;
    });

    cards = filtered.map(s => {
      const pub = s.publisher ? `<div class="series-card-publisher">${esc(s.publisher.toUpperCase())}</div>` : '';
      const total = (s.owned ?? 0) + (s.missing ?? 0);
      const pct = total ? Math.round((s.owned / total) * 100) : 0;
      const color = s.missing > 0 ? 'var(--amb)' : 'var(--grn)';
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
            <div class="series-card-count" style="color:${color}">${s.owned}/${total}</div>
          </div>
          ${pub}
        </div>
      `;
    }).join('');
    pagination = '';
  }

  document.getElementById('browse-results').innerHTML = `
    ${cards ? `<div class="series-grid">${cards}</div>` : '<div class="state-msg">No series found.</div>'}
    ${pagination}
  `;
}

function navigateKomgaSeries(jsonStr) {
  const s = JSON.parse(jsonStr);
  navigate('series-detail', { komgaId: s.id, komgaData: s });
}

function browsePage(page) {
  if (browseState.filter !== 'all') return;
  browseState.page = page;
  _loadBrowsePage();
}

function browseSearch(val) {
  clearTimeout(browseState.searchTimer);
  browseState.searchTimer = setTimeout(() => {
    browseState.search = val;
    browseState.page = 0;
    _loadBrowsePage();
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

  const summaryBar = `
    <div class="match-summary-bar">
      <div class="match-summary-stat">
        <span class="match-count match-high">${counts.high}</span>
        <span class="match-label">Auto-matched</span>
      </div>
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

  // High confidence
  if (groups.high.length) {
    const cards = groups.high.map(c => _matchCard(c, true)).join('');
    html += `
      <div class="match-section">
        <div class="match-section-header">
          <span class="match-section-title">Auto-matched <span class="match-section-count">${groups.high.length}</span></span>
          <button class="btn btn-primary btn-sm" onclick="confirmAllHigh()">Confirm All</button>
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
}

function _matchCard(c, autoChecked) {
  const candidates = c.candidates || [];
  const pct = Math.round(c.score * 100);

  let picker = '';
  if (!autoChecked && candidates.length) {
    picker = `<div class="match-candidates">
      ${candidates.map((r, i) => `
        <label class="match-candidate-row">
          <input type="radio" name="mc_${esc(c.komga_series_id)}" value="${r.id}"
            ${i === 0 ? 'checked' : ''}>
          <span class="match-candidate-name">${esc(r.name)}</span>
          <span class="match-candidate-meta">${esc(r.publisher || '')}${r.year ? ' · ' + r.year : ''}</span>
          <span class="match-candidate-score">${Math.round(r.score * 100)}%</span>
        </label>
      `).join('')}
    </div>`;
  } else if (autoChecked) {
    picker = `<div class="match-auto-label">
      ${esc(c.metron_title)}
      ${c.metron_year ? `<span class="match-year">${c.metron_year}</span>` : ''}
    </div>`;
  }

  return `
    <div class="match-card" id="mc_${esc(c.komga_series_id)}">
      <div class="match-card-img-wrap">
        <img src="/api/komga/series/${esc(c.komga_series_id)}/thumbnail" alt="${esc(c.komga_title)}"
          onerror="this.style.opacity='0.2'">
      </div>
      <div class="match-card-body">
        <div class="match-card-title">${esc(c.komga_title)}</div>
        <div class="match-card-meta">${esc(c.komga_publisher || '')}${c.komga_year ? ' · ' + c.komga_year : ''}</div>
        <div class="match-score-bar-wrap">
          <div class="match-score-bar" style="width:${pct}%"></div>
          <span class="match-score-pct">${pct}%</span>
        </div>
        ${picker}
        <div class="match-card-actions">
          <button class="btn btn-primary btn-sm"
            onclick="confirmCard('${esc(c.komga_series_id)}', this, ${autoChecked ? c.metron_id : 0})">
            Confirm
          </button>
          <button class="btn btn-ghost btn-sm"
            onclick="openManualMatch('${esc(c.komga_series_id)}', '${esc(c.komga_title)}')">
            Change
          </button>
          <button class="btn btn-ghost btn-sm"
            onclick="rejectCandidate('${esc(c.komga_series_id)}', this)">
            Skip
          </button>
        </div>
      </div>
    </div>
  `;
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
  const pass = document.getElementById('s-komga-pass').value;
  const mpass = document.getElementById('s-metron-pass').value;
  if (pass)  updates.komga_pass  = pass;
  if (mpass) updates.metron_pass = mpass;

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
  document.getElementById('modal').classList.add('hidden');
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
