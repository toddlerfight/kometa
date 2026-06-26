// --- API ---

let _appConfig = {};
let _issueModalPollTimer = null;

// Per-item stagger for cascading grid/list entrances (ms). One value so cascades
// feel consistent everywhere. Pairs with the CSS motion tokens in style.css :root.
const STAGGER_MS = 40;

// "Open in Komga" links get handed to the BROWSER, not the server. _appConfig.komga_url
// is the SERVER's view of Komga — a LAN IP frozen in settings. Hand that to a browser
// sitting in New Zealand and it dies screaming into the void. So: keep Komga's scheme +
// port from config, but swap the host for whatever host actually loaded THIS page. The
// browser already proved it can reach that host (it's reading this from it), so Komga on
// the same host answers too. LAN, Tailscale, localhost — the link follows you home.
function komgaBase() {
  try {
    const u = new URL(_appConfig.komga_url);
    u.hostname = location.hostname;
    return u.origin;
  } catch {
    return `${location.protocol}//${location.hostname}:8585`;
  }
}

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
let _toastStickyUntil = 0;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  if (!el) return;
  const now = Date.now();
  // errors hold the slot for 5s — a routine toast can't clobber a fresh failure
  if (type !== 'error' && now < _toastStickyUntil) return;
  el.textContent = msg;
  el.className = `toast-show${type === 'error' ? ' toast-error' : ''}`;
  _toastStickyUntil = type === 'error' ? now + 5000 : 0;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = 'toast-hidden'; }, type === 'error' ? 5000 : 3000);
}

// --- Router ---

let currentView = 'series';
let currentParams = {};

// Rows synced before 2026-06-10 can carry LOCG's no-cover placeholder as
// metron_image — a RELATIVE path that 404s against our origin every render.
// Treat anything that isn't an absolute real-art URL as no art at all.
const _metronArt = i => (i.metron_image && i.metron_image.startsWith('http')
  && !i.metron_image.includes('no-cover')) ? i.metron_image : null;
let detailTab = 'all';
let detailSortDesc = true;
let _autoTabFor = null;   // series id we've already made the B1 trades-default call for

function navigate(view, params = {}) {
  currentView = view;
  currentParams = params;
  if (view !== 'series-detail') {
    detailTab = 'all';
    _autoTabFor = null;   // leaving detail — let the next entry re-decide its default tab
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
    _autoTabFor = null;   // leaving detail — let the next entry re-decide its default tab
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
  // The VIEWER's own calendar date. Drives the "today/upcoming" line and the TODAY
  // label, so a release shows TODAY on YOUR date — not a day late because the US clock
  // hasn't caught up. The missing flip uses _usToday (below), so it still won't go
  // "missing" before the US release has actually passed.
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function _usToday() {
  // US (Pacific) date — used ONLY for the "missing" boundary. Comic store_dates are US
  // release dates; Pacific is the most forgiving US zone, so an issue won't flip to
  // "missing" until the date has fully passed across the States (your AEST date runs
  // ahead, so using it alone would mark things missing before they've even dropped).
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
}

function _fmtReleaseDate(dateStr) {
  if (dateStr === _localToday()) return 'TODAY';
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

function fmtNum(n) {
  const f = parseFloat(n);
  return Number.isInteger(f) ? String(f) : String(f);
}

function issueStatus(issue) {
  const lt = _localToday(), ut = _usToday();
  if (issue.owned) return 'owned';
  if (!issue.store_date) return 'unknown';
  if (issue.store_date > lt) return 'upcoming';   // future on YOUR calendar
  if (issue.store_date >= ut) return 'today';      // out today (your date) — or still dropping in the US
  return 'missing';                                 // only once it's passed in the US too
}

function fmtDayDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-AU', { weekday: 'long', month: 'short', day: 'numeric' });
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
  if (pct >= 1)   return 'var(--pri)';
  if (pct >= 0.9) return 'var(--pri)';
  return 'var(--amb)';
}

function countColor(owned, total) {
  if (!total) return 'var(--tq)';
  return owned >= total ? 'var(--pri)' : 'var(--tm)';
}

// --- Series List ---

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
        // Only repaint if the user is STILL on this series — and only when the
        // sync changed something visible. The old else-branch here painted
        // renderSeries() (a dead, router-unreachable page) over WHATEVER view
        // you were on whenever a background sync landed — the infamous
        // "page keeps bouncing to the library by itself" poltergeist.
        if (currentView === 'series-detail' && currentParams.id === id) {
          const changed = !before
            || s.owned !== before.owned || s.missing !== before.missing
            || s.upcoming !== before.upcoming || s.next_release !== before.next_release
            || (s.issues || []).length !== (before.issues || []).length;
          if (changed) await renderSeriesDetail(id);
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

let browseState = { search: '', searchTimer: null, filter: 'all', _cache: null, sortKey: 'date', sortDir: { date: 'asc' } };

async function renderLibraryBrowse() {
  document.getElementById('topbar-title').textContent = 'Library';
  // No Sync All button — the scheduler syncs everything at 5/12/17, stale series
  // self-sync on view, and series detail has its own Sync. The machine does the
  // work; a global button here was a comfort blanket with no feedback.
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-primary btn-sm" onclick="showAddWizard()">+ Add Series</button>
  `;
  browseState.search  = '';
  browseState.filter  = 'monitored';
  browseState._cache  = null;
  browseState.sortKey = 'date';
  browseState.sortDir = { date: 'asc' };   // nearest release first (soonest at top)
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
  return browseState.sortKey === 'date' && (browseState.sortDir.date ?? 'asc') === 'asc';
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
  // Nothing tracked yet on a configured install? Open the one door they'd open
  // anyway — but give them ~5s to read the empty state and reach for it themselves
  // first. Re-check everything when the timer fires: they may have navigated off,
  // added a series, or opened another modal in the meantime.
  clearTimeout(_autoWizardTimer);
  if (browseState._cache.length === 0 && _appConfig.comics_root_ok) {
    _autoWizardTimer = setTimeout(() => {
      if (currentView === 'library'
          && (browseState._cache?.length ?? 0) === 0
          && _appConfig.comics_root_ok
          && document.getElementById('modal-backdrop')?.classList.contains('hidden')) {
        showAddWizard();
      }
    }, 5000);
  }
}
let _autoWizardTimer = null;

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
    const dir = (sortDir.date ?? 'asc') === 'asc' ? 1 : -1;
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

  const cards = filtered.map((s, i) => {
    const pub   = s.publisher ? `<div class="series-card-publisher">${esc(s.publisher.toUpperCase())}</div>` : '';
    const total = (s.owned ?? 0) + (s.missing ?? 0);
    const pct   = total ? Math.round((s.owned / total) * 100) : 0;
    const color = s.missing > 0 ? 'var(--amb)' : (total > 0 ? 'var(--pri)' : 'var(--tq)');
    const nextRelease = s.next_release
      ? `<div class="series-card-next-release">${_fmtReleaseDate(s.next_release)}</div>` : '';
    const thumbSrc  = s.card_image || `/api/series/${s.id}/thumbnail`;
    const thumbFall = s.card_image  ? `this.src='/api/series/${s.id}/thumbnail'` : `this.style.opacity='0.15'`;
    return `
      <div class="series-card card-cascade" style="animation-delay:${Math.min(i,14)*STAGGER_MS}ms" tabindex="0" role="button"
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
    if (detailTab === 'upcoming') return st === 'upcoming' || st === 'today';
    return true;
  }).sort((a, b) => detailSortDesc ? b.number - a.number : a.number - b.number);

  return filtered.map(issue => _issueTileHtml(s, issue)).join('');
}

function _issueTileHtml(s, issue) {
    const st = issueStatus(issue);
    const num = `#${fmtNum(issue.number)}`;
    const dateBadge = st === 'today'
      ? `<div class="series-card-next-release">TODAY</div>`
      : (st === 'upcoming' && issue.store_date
          ? `<div class="series-card-next-release">${issue.store_date.replace(/-/g, '/')}</div>`
          : '');
    let inner = '';
    if (st === 'owned') {
      // variant_cover (your chosen cover) wins — Komga's thumbnail is frozen until
      // it re-scans the modified CBZ, so show the pick directly.
      const thumbSrc = issue.variant_cover
        ? issue.variant_cover
        : issue.komga_book_id
          ? `/api/book/${issue.komga_book_id}/thumbnail`
          : `/api/series/${s.id}/issues/${issue.number}/thumbnail`;
      inner = `<div class="issue-tile-img">
        <img src="${esc(thumbSrc)}" alt="${num}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">
      </div>`;
    } else {
      // variant_cover = your saved variant pick (not-yet-downloaded issues) — show it
      // here too so the grid tile matches the modal, falling back to the solicit cover.
      // No client-side art at all? Ask the server — its thumbnail route walks the
      // whole fallback chain (metron → LOCG main → variant art). An issue with
      // genuinely zero art anywhere 404s and the img removes itself, same blank as before.
      const thumbSrc = issue.variant_cover
        || _metronArt(issue)
        || `/api/series/${s.id}/issues/${issue.number}/thumbnail`;
      inner = `<div class="issue-tile-img ${st}">
        <img src="${esc(thumbSrc)}" alt="${num}" loading="lazy"
          onerror="this.remove()">${dateBadge}
      </div>`;
    }
    const searchBtn = (st === 'missing' || st === 'today')
      ? `<button class="issue-tile-search" title="Search for this issue" data-dl="${s.id}:${issue.number}"
           onclick="event.stopPropagation(); searchIssue(${s.id}, ${issue.number}, this)">↓</button>`
      : '';
    return `<div class="issue-tile" data-num="${issue.number}" title="${esc(s.title)} ${num}"
      onclick="showIssueModal(${s.id}, ${issue.number})">
      ${inner}
      <div class="issue-tile-num">${num}</div>
      ${searchBtn}
    </div>`;
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

// Story arc detail — its issues span titles (Batman, Detective, …) and live in
// arc_issues, not issue_status, so it renders a cross-title reading-order table
// instead of the single-title issue grid.
function renderArcDetail(s) {
  const seriesBg = document.getElementById('series-bg');
  if (seriesBg) seriesBg.classList.add('hidden');
  const meta = ['◆ STORY ARC', s.publisher ? s.publisher.toUpperCase() : '', s.year_began].filter(Boolean).join('  •  ');
  const ai = s.arc_issues || [];
  const total = ai.length;
  const collChip = s.collected ? `<span class="chip chip-collected" title="${esc(s.collection?.name || '')}">◆ collected</span>` : '';
  const chips = (total
    ? `<span class="chip ${s.owned < total ? 'chip-missing' : 'chip-complete'}">${s.owned}/${total}</span>`
    : '') + collChip;
  const rows = ai.map(i => {
    // Owned single > available via the collected edition > genuinely missing.
    const st = i.owned
      ? '<span class="arc-st owned">✓ owned</span>'
      : s.collected
      ? '<span class="arc-st coll">◆ in collection</span>'
      : '<span class="arc-st missing">○ missing</span>';
    return `<tr>
      <td class="arc-num">${i.reading_order}</td>
      <td class="arc-src">${esc(i.source_title || '')} <span class="arc-iss">#${esc(String(i.number ?? '?'))}</span></td>
      <td class="arc-story">${esc(i.story_title || '')}</td>
      <td class="arc-stcell">${st}</td>
    </tr>`;
  }).join('');
  const collBanner = (s.collected && s.collection) ? `
    <div class="arc-collected-note" onclick="navigate('series-detail',{id:${s.collection.series_id}})">
      ◆ Owned as a collected edition — <strong>${esc(s.collection.name)}</strong>. The readlist builds from its volumes.
    </div>` : '';
  const body = total ? `
    <table class="arc-table">
      <thead><tr><th>#</th><th>Source</th><th>Story</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>` : `<div class="state-msg" style="padding:28px 0;font-size:12px;color:var(--tq)">Pulling reading order from ComicVine…</div>`;
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
    <div class="arc-body">
      <div class="arc-tabs"><div class="issue-tab active">reading order</div></div>
      <div class="arc-sec-label">Reading order · across all participating titles</div>
      ${collBanner}
      ${body}
      <div style="margin-top:22px;padding-top:18px;border-top:1px solid var(--bd);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="fulfillArc(${s.id}, this)">Fulfill arc</button>
        <button class="btn btn-ghost" onclick="buildArcReadlist(${s.id}, this)">Build Komga Readlist</button>
        <button class="btn btn-ghost btn-sm" onclick="refreshArcOwnership(${s.id}, this)">Refresh ownership</button>
      </div>
    </div>
  `);
  // Just-added arc whose background populate hasn't landed yet — re-fetch once.
  if (!total) setTimeout(() => {
    if (currentView === 'series-detail' && currentParams.id === s.id) renderSeriesDetail(s.id);
  }, 4000);
}

async function fulfillArc(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Queueing…'; }
  try {
    const r = await api.post(`/api/series/${id}/fulfill`, {});
    if (r.queued === 0) {
      showToast(r.owned >= r.total ? 'Arc already complete — nothing to pull' : 'Nothing missing to queue');
    } else {
      showToast(`Fulfilling arc — queued ${r.queued} missing issue${r.queued === 1 ? '' : 's'} into their runs`);
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Fulfill arc'; }
  } catch (e) {
    showToast('Fulfill failed — ' + (e?.message || e), 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Fulfill arc'; }
  }
}

async function buildArcReadlist(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Building…'; }
  try {
    const r = await api.post(`/api/series/${id}/readlist`, {});
    showToast(`Readlist "${r.name}" → Komga · ${r.books} book${r.books === 1 ? '' : 's'}${r.updated ? ' (updated)' : ''}`);
    if (btn) { btn.disabled = false; btn.textContent = 'Rebuild Komga Readlist'; }
  } catch (e) {
    showToast('Readlist failed — ' + (e?.message || e), 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Build Komga Readlist'; }
  }
}

async function refreshArcOwnership(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Checking Komga…'; }
  try {
    const r = await api.post(`/api/series/${id}/resolve-arc`, {});
    const note = r.collection ? ` · collected in "${r.collection.name}"` : '';
    showToast(`Ownership refreshed · ${r.owned}/${r.total} singles${note}`);
    if (currentView === 'series-detail' && currentParams.id === id) renderSeriesDetail(id);
  } catch (e) {
    showToast('Refresh failed — ' + (e?.message || e), 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh ownership'; }
  }
}

async function renderSeriesDetail(id) {
  setTopbar(`<button class="btn btn-ghost btn-sm" onclick="navigate('library')">← Library</button>`);
  setApp('<div class="state-msg">Loading...</div>');

  const s = await api.get(`/api/series/${id}`);
  _detailSeries = s;
  if (s.kind === 'arc') return renderArcDetail(s);

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

  // B1: a trade-only series (no singles, but collected editions exist) lands you on
  // Trades — otherwise you hit an empty Issues grid while the real content hides one
  // tab over. One-shot per entry (_autoTabFor) so a manual tab pick afterward sticks.
  if (_autoTabFor !== id) {
    if (total === 0 && s.has_trades) detailTab = 'trades';
    _autoTabFor = id;
  }

  const chips = [
    released > 0 ? `<span class="chip ${s.owned < released ? 'chip-missing' : 'chip-complete'}">${s.owned}/${released}</span>` : '',
    s.upcoming ? `<span class="chip chip-upcoming">${s.upcoming} upcoming</span>` : '',
  ].filter(Boolean).join('');

  const pullBtn = `<button class="btn btn-sm ${s.on_pull_list ? 'btn-primary' : 'btn-ghost'}"
    onclick="togglePullList(${s.id}, ${!s.on_pull_list})">Pull</button>`;

  const tabs = ['all','owned','missing','upcoming','trades','arcs'].map(t => {
    // Trades tab carries a count badge when collected editions exist (from cache).
    const badge = (t === 'trades' && s.trade_count)
      ? `<span class="tab-badge">${s.trade_count}</span>`
      : (t === 'arcs' && s.arc_count) ? `<span class="tab-badge">${s.arc_count}</span>` : '';
    return `<div class="issue-tab ${detailTab === t ? 'active' : ''}" onclick="setDetailTab('${t}', ${id})">${t}${badge}</div>`;
  }).join('');

  const tiles = (detailTab === 'trades' || detailTab === 'arcs') ? '' : buildIssueTiles(s);

  const seriesBg = document.getElementById('series-bg');
  const seriesBgImg = document.getElementById('series-bg-img');
  // Random issue cover as the backdrop — different each visit, not always the same one.
  const _covers = (s.issues || []).map(i => i.metron_image).filter(Boolean);
  const _bg = _covers.length
    ? _covers[Math.floor(Math.random() * _covers.length)]
    : `/api/series/${s.id}/thumbnail`;
  seriesBgImg.style.backgroundImage = `url("${_bg}")`;
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
    ${detailTab === 'trades'
      ? `<div id="trades-panel" class="trades-body"><div class="state-msg" style="padding:20px 0;font-size:11px">Looking for trades…</div></div>`
      : detailTab === 'arcs'
      ? `<div id="arcs-panel" class="arcs-body"><div class="state-msg" style="padding:20px 0;font-size:11px">Looking for arcs…</div></div>`
      : `<div class="issue-grid">${tiles || `<div class="state-msg" style="grid-column:1/-1">${
          total === 0 && s.has_trades ? 'No single issues — collected in Trades →'
          : (s.metron_series_id || s.locg_series_id) && total === 0 ? 'Syncing issues…'
          : 'Nothing here.'}</div>`}</div>`}
  `);

  if (detailTab === 'trades') _loadTradesPanel(id);
  if (detailTab === 'arcs') _loadArcsPanel(id);

  // Don't poll a trade-only series for singles that will never come (has_trades +
  // empty issue list = collected editions only) — that's the 'Syncing issues…' spinner
  // that never resolved. Only poll when issues are genuinely still inbound.
  if ((s.metron_series_id || s.locg_series_id) && total === 0 && !s.has_trades && detailTab !== 'trades') {
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

async function _loadArcsPanel(id) {
  let data;
  try {
    data = await api.get(`/api/series/${id}/arcs`);
  } catch (e) {
    const b = document.getElementById('arcs-panel');
    if (b) b.innerHTML = `<div class="state-msg" style="padding:20px 0;font-size:11px;color:var(--amb)">Lookup failed: ${esc(String(e))}</div>`;
    return;
  }
  const body = document.getElementById('arcs-panel');
  if (!body || detailTab !== 'arcs' || currentParams.id !== id) return;
  const arcs = data.arcs || [];
  _arcsPanelData = arcs; _arcsPanelSeriesId = id;
  if (!arcs.length) {
    body.innerHTML = `<div class="state-msg" style="padding:20px 0;font-size:11px">No story arcs found for this series (Wikipedia has none, or it's not yet covered).</div>`;
    return;
  }
  body.innerHTML = arcs.map((a, i) => _arcRowHtml(a, i)).join('');
}

let _arcsPanelData = [];
let _arcsPanelSeriesId = null;

function _arcRowHtml(a, i) {
  if (a.tracked) {
    const ntitles = (a.source_titles || []).length;
    const complete = a.issue_count > 0 && a.owned_count >= a.issue_count;
    return `<div class="arc-list-row" onclick="navigate('series-detail',{id:${a.id}})">
      <span class="arc-list-dia">◆</span>
      <span class="arc-list-name">${esc(a.name)}</span>
      <span class="arc-list-meta">${a.issue_count} issues · ${ntitles} title${ntitles === 1 ? '' : 's'} · tracked</span>
      <span class="arc-list-count${complete ? ' ok' : ''}">${a.owned_count}/${a.issue_count}</span>
    </div>`;
  }
  // Discovered (not yet tracked) — clicking populates on demand. ComicVine arcs
  // carry a cv_arc_id (precise order); Wikipedia ones carry an issue range.
  const range = (a.first_issue && a.last_issue) ? `#${a.first_issue}–${a.last_issue}` : '';
  const n = (a.first_issue && a.last_issue) ? `${a.last_issue - a.first_issue + 1} issues` : '';
  const meta = a.source === 'comicvine' ? 'ComicVine' : [range, n].filter(Boolean).join(' · ');
  return `<div class="arc-list-row arc-discovered" onclick="openDiscoveredArc(${i}, this)">
    <span class="arc-list-dia dim">◇</span>
    <span class="arc-list-name">${esc(a.name)}</span>
    <span class="arc-list-meta">${meta}</span>
    <span class="arc-list-count view">view →</span>
  </div>`;
}

async function openDiscoveredArc(i, row) {
  const a = _arcsPanelData[i];
  if (!a) return;
  const cnt = row && row.querySelector('.arc-list-count');
  if (cnt) cnt.textContent = 'opening…';
  if (row) row.style.opacity = '0.5';
  try {
    const arc = await api.post(`/api/series/${_arcsPanelSeriesId}/arcs/populate`,
      { name: a.name, cv_arc_id: a.cv_arc_id, first_issue: a.first_issue, last_issue: a.last_issue });
    navigate('series-detail', { id: arc.id });
  } catch (e) {
    showToast('Couldn’t open arc — ' + (e?.message || e), 'error');
    if (row) row.style.opacity = '';
    if (cnt) cnt.textContent = 'view →';
  }
}

async function _loadTradesPanel(id) {
  let data;
  try {
    data = await api.get(`/api/series/${id}/trades`);
  } catch (e) {
    const b = document.getElementById('trades-panel');
    if (b) b.innerHTML = `<div class="state-msg" style="padding:20px 0;font-size:11px;color:var(--amb)">Lookup failed: ${esc(String(e))}</div>`;
    return;
  }
  const body = document.getElementById('trades-panel');
  // Bail if the user switched tabs / left while LOCG was answering.
  if (!body || detailTab !== 'trades' || currentParams.id !== id) return;
  const trades = data.trades || [];
  if (!trades.length) {
    body.innerHTML = `<div class="state-msg" style="padding:20px 0;font-size:11px">${
      data.reason === 'no_locg_id' ? 'No LOCG link for this series — can\'t look up trades.' : 'No collected editions found.'}</div>`;
    return;
  }
  // TPBs first (the common case), then HCs. Volume order within each.
  const ordered = trades.slice().sort((a, b) =>
    (a.format === b.format ? 0 : a.format === 'TPB' ? -1 : 1) ||
    ((a.vol ?? a.vol_range?.[0] ?? 999) - (b.vol ?? b.vol_range?.[0] ?? 999)));
  // Cache by locg_id so the tile's click can recover the full trade object.
  _tradesByLocg = {};
  for (const t of ordered) if (t.locg_id) _tradesByLocg[t.locg_id] = t;
  body.innerHTML = `<div class="issue-grid">${ordered.map(_tradeTileHtml).join('')}</div>`;
}

let _tradesByLocg = {};

function _tradeTileHtml(t) {
  const tag = t.vol_range ? `Vol ${t.vol_range[0]}–${t.vol_range[1]}`
            : t.vol ? `Vol ${t.vol}` : t.format;
  // Same tile skeleton as issues so the grid stays visually identical. The format
  // (TPB/HC) rides in the corner badge slot; the volume is the bottom label.
  const cover = t.cover ? `<img src="${esc(t.cover)}" alt="${esc(tag)}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">` : '';
  const click = t.locg_id ? ` onclick="showTradeModal('${t.locg_id}')"` : '';
  // Owned (file on disk) → no download arrow, owned styling. Otherwise the same
  // ↓ arrow missing singles get; stopPropagation so the tile click doesn't fire.
  const dlBtn = (t.locg_id && !t.owned)
    ? `<button class="issue-tile-search" title="Download this trade" data-dl="trade:${t.locg_id}"
         onclick="event.stopPropagation(); tradeDownload('${t.locg_id}', this)">↓</button>`
    : '';
  const ownedBadge = t.owned ? `<div class="trade-owned-check" title="On disk">✓</div>` : '';
  // Unowned → amber 'missing' outline, exactly like a missing issue.
  const stateCls = t.owned ? '' : ' missing';
  return `<div class="issue-tile trade-tile${t.owned ? ' owned' : ''}" title="${esc(t.title)}"${click}>
    <div class="issue-tile-img${stateCls}${t.cover ? '' : ' unknown'}">
      ${cover}
      <div class="series-card-next-release trade-fmt-${t.format.toLowerCase()}">${t.format}</div>
      ${ownedBadge}
    </div>
    <div class="issue-tile-num">${tag}</div>
    ${dlBtn}
  </div>`;
}

function _tradeFooterAction(t, locgId) {
  if (t.owned) {
    // Two separate facts: owned (on disk) and whether Komga can read it.
    if (t.komga_book_id && _appConfig.komga_url) {
      const url = `${komgaBase()}/book/${esc(t.komga_book_id)}/read`;
      return `<a class="btn btn-primary" href="${url}" target="_blank" rel="noopener">Open in Komga</a>`;
    }
    return `<span class="trade-owned-note">On disk${_appConfig.komga_url ? ' · not yet in Komga' : ''}</span>`;
  }
  return `<button class="btn btn-primary" id="trade-dl-btn" onclick="tradeDownload('${esc(locgId)}')">Download</button>`;
}

async function showTradeModal(locgId) {
  const t = _tradesByLocg[locgId];
  if (!t) return;
  const s = _detailSeries;
  const tag = t.vol_range ? `Vols ${t.vol_range[0]}–${t.vol_range[1]}`
            : t.vol ? `Vol ${t.vol}` : t.format;
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      <div class="issue-modal-cover">
        ${t.cover
          ? `<img src="${esc(t.cover)}" alt="${esc(tag)}" onerror="this.style.opacity='0.1'">`
          : `<div class="issue-modal-no-cover"></div>`}
      </div>
      <div class="issue-modal-info">
        <div class="issue-modal-num">${esc(tag)}</div>
        <div class="issue-modal-series">${esc(s.title)}</div>
        <div class="issue-modal-meta">${esc([s.publisher, s.year_began].filter(Boolean).join(' · '))}</div>
        <div style="margin:8px 0"><span class="chip trade-chip-${t.format.toLowerCase()}">${t.format}</span></div>
        <div class="issue-modal-panel active">
          <div class="issue-modal-details" id="issue-modal-details">
            <div class="state-msg" style="font-size:11px;padding:8px 0">Loading details…</div>
          </div>
        </div>
      </div>
    </div>
    <div class="modal-footer" id="trade-modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${_tradeFooterAction(t, locgId)}
    </div>
  `);
  try {
    const d = await api.get(`/api/trade/${encodeURIComponent(locgId)}/details`);
    _renderIssueDetails(d.desc, d.credits || []);
  } catch { _renderIssueDetails('', []); }
}

async function tradeDownload(locgId, btn) {
  const t = _tradesByLocg[locgId];
  if (!t) return;
  // btn omitted → came from the modal's Download button.
  btn = btn || document.getElementById('trade-dl-btn');
  const isArrow = btn?.classList.contains('issue-tile-search');
  const label = t.vol_range ? `Vols ${t.vol_range[0]}–${t.vol_range[1]}`
              : t.vol ? `Vol ${t.vol}` : t.format;
  if (btn) {
    btn.disabled = true;
    if (isArrow) btn.textContent = '⋯';
    else { btn.classList.add('btn-working'); btn.textContent = 'Queuing…'; }
  }
  try {
    // Just enqueue — it's a Kometa acquisition now. Progress lives in Activity,
    // same as an issue download.
    const r = await api.post(`/api/series/${_detailSeries.id}/trades/download`,
      { locg_id: locgId, title: _detailSeries.title, vol: t.vol, vol_range: t.vol_range,
        cover: t.cover, edition_title: t.title });
    if (btn) btn.classList.remove('btn-working');
    if (r.reason === 'no_folder') {
      _tradeBtnReset(btn, isArrow); showToast('Set a folder for this series first'); return;
    }
    if (btn) { if (isArrow) { btn.textContent = '✓'; btn.classList.add('found'); } else btn.textContent = '✓ Queued'; }
    showToast(`${label} queued — track it in Activity`);
  } catch (e) {
    _tradeBtnReset(btn, isArrow);
    showToast(`Queue failed: ${esc(String(e))}`);
  }
}

function _tradeBtnReset(btn, isArrow) {
  if (!btn) return;
  btn.disabled = false;
  btn.classList.remove('btn-working');
  btn.textContent = isArrow ? '↓' : 'Download';
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
let _wizardLastQuery = '';   // dedupe — don't re-search a query we already answered
let _wizardSeq = 0;          // request token — drop stale results that land out of order
let _wizardHi = -1;   // keyboard-highlighted result index

function _wizardKey(e) {
  const n = _wizardResults.length;
  if (e.key === 'ArrowDown' && n) {
    e.preventDefault(); _wizardHi = (_wizardHi + 1) % n; _wizardPaintHi();
  } else if (e.key === 'ArrowUp' && n) {
    e.preventDefault(); _wizardHi = (_wizardHi - 1 + n) % n; _wizardPaintHi();
  } else if (e.key === 'Enter') {
    if (_wizardHi >= 0 && n) wizardPickSeries(_wizardHi);
    else wizardSearch();
  }
}

function _wizardPaintHi() {
  document.querySelectorAll('.wizard-result').forEach((el, i) =>
    el.classList.toggle('kbd-hi', i === _wizardHi));
  document.querySelector('.wizard-result.kbd-hi')?.scrollIntoView({ block: 'nearest' });
}
let _wizardState = { idx: -1, metronId: null, source: 'metron', locgId: null };

function showAddWizard() {
  // Can't track or file a series with nowhere to put it. If the comics folder
  // isn't usable, channel the user to set it first — just-in-time, not a gate.
  if (!_appConfig.comics_root_ok) { _showComicsRootSetup(); return; }
  _wizardResults = [];
  _wizardLastQuery = '';
  _wizardState = { idx: -1, metronId: null, source: 'metron', locgId: null };
  showModal(`
    <div class="modal-title">Add Series</div>
    <div class="wizard-search-row">
      <input class="search-input" id="wizard-search" placeholder="Search for a series…" autocomplete="off"
        oninput="_wizardInput(this.value)" onkeydown="_wizardKey(event)">
      <button class="btn btn-primary" onclick="wizardSearch()">Search</button>
    </div>
    <div class="wizard-results" id="wizard-results">
      <div class="state-msg" style="padding:16px 0;font-size:11px">Start typing to search…</div>
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

function browseSettingsRoot() {
  // Same filesystem browser (with New Folder), but for the Settings field:
  // selecting fills the input and autosaves it like any other edit.
  _fbScope = 'fs';
  _fbCallback = (path) => {
    closeModal();
    const i = document.getElementById('f-comics-root');
    if (i) { i.value = path; _settingsChanged(i); }
  };
  showModal('<div class="modal-title">Select Folder</div><div class="fb-loading">Loading…</div>');
  _fbNav('');
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

// A search hit that's really a collected edition (omnibus/deluxe/etc), not a series.
// Trades live in a series' Trades tab — flag these so they aren't mistaken for one.
function _isCollectedResult(r) {
  if (r.kind === 'arc') return false;
  // 'absolute' deliberately excluded — DC's current Absolute line is ongoing series,
  // not the old collected-edition format. Keep only unambiguous collected-edition words.
  return /\b(omnibus|deluxe edition|compendium|the complete|collected edition|library edition)\b/i.test(r.series || r.name || '');
}

function _renderWizardResults(results, q) {
  const container = document.getElementById('wizard-results');
  if (!container) return;
  const ql = q.toLowerCase();
  results.sort((a, b) => {
    // Arcs first — for an event that fell through to CV, the arc (whole cross-title
    // story) is usually what you want over a single collected-edition volume.
    const aArc = a.kind === 'arc', bArc = b.kind === 'arc';
    if (aArc !== bArc) return aArc ? -1 : 1;
    const at = (a.series || a.name || '').toLowerCase();
    const bt = (b.series || b.name || '').toLowerCase();
    const aExact = at === ql, bExact = bt === ql;
    const aPrefix = at.startsWith(ql), bPrefix = bt.startsWith(ql);
    if (aExact !== bExact) return aExact ? -1 : 1;
    if (aPrefix !== bPrefix) return aPrefix ? -1 : 1;
    // A collected edition sinks below the actual series it collects.
    const aColl = _isCollectedResult(a), bColl = _isCollectedResult(b);
    if (aColl !== bColl) return aColl ? 1 : -1;
    return 0;
  });
  _wizardResults = results.slice(0, 15);
  _wizardHi = -1;   // fresh results, fresh keyboard cursor

  const paint = () => {
    const el = document.getElementById('wizard-results');
    if (!el) return;
    el.innerHTML = _wizardResults.length
      ? _wizardResults.map((r, i) => `
          <div class="wizard-result wizard-result-enter" style="animation-delay:${i * STAGGER_MS}ms" onclick="wizardPickSeries(${i})">
            <img class="wizard-result-thumb" src="${r.source === 'locg' ? esc(r.cover || '') : `/api/metron/series/${r.id}/thumbnail`}" alt=""
              onerror="this.style.opacity=0" loading="lazy">
            <div class="wizard-result-text">
              <div class="wizard-result-title">${esc(r.series || r.name || '')}${r.kind === 'arc' ? ' <span class="locg-badge">◆ ARC</span>' : _isCollectedResult(r) ? ' <span class="locg-badge collected-badge">◆ COLLECTED</span>' : r.source === 'locg' ? ' <span class="locg-badge">LOCG</span>' : ''}</div>
              <div class="wizard-result-meta">${esc(r.publisher?.name || '')}${r.kind === 'arc' ? ' · story arc' : _isCollectedResult(r) ? ' · collected edition — lives in a series’ Trades' : ''}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}</div>
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

// Type-ahead: fire one search per typing PAUSE, not per keystroke. 3-char floor +
// 400ms debounce keeps LOCG's anonymous (rate-limited) path from getting smacked —
// a 7-letter title is one request, not seven. Manual Search button still calls
// wizardSearch() directly for the impatient.
function _wizardInput(val) {
  clearTimeout(_wizardSearchTimer);
  const q = (val || '').trim();
  if (q.length < 3) {
    _wizardLastQuery = '';
    if (!_wizardResults.length) {
      const el = document.getElementById('wizard-results');
      if (el) el.innerHTML = `<div class="state-msg" style="padding:16px 0;font-size:11px">Keep typing…</div>`;
    }
    return;
  }
  _wizardSearchTimer = setTimeout(() => wizardSearch(), 400);
}

async function wizardSearch() {
  const q = document.getElementById('wizard-search')?.value?.trim() || '';
  if (!document.getElementById('wizard-results') || !q) return;
  if (q === _wizardLastQuery) return;   // already answered this exact query
  _wizardLastQuery = q;
  const seq = ++_wizardSeq;             // anything older than this is stale on return
  try {
    _wizardStatus('Searching…');
    // ComicVine runs on EVERY search (in parallel) — an arc is a distinct
    // event-level result that should surface ALONGSIDE LOCG's collected editions,
    // not just when LOCG is empty. We always merge the arcs in; CV's collected-
    // edition VOLUMES only show as the LOCG-empty fallback (to avoid noise).
    const cvP = api.get(`/api/search/comicvine?q=${encodeURIComponent(q)}`).catch(() => []);

    // Metron is optional — swallow its error and fall through to LOCG (key-free).
    let metronResults = [];
    try {
      metronResults = await api.get(`/api/search/metron?q=${encodeURIComponent(q)}`);
    } catch (e) {
      console.warn('Metron search unavailable, falling back to LOCG:', e);
    }
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    if (metronResults.length) {
      const arcs = (await cvP).filter(r => r.kind === 'arc');
      _renderWizardResults([...arcs, ...metronResults], q); return;
    }

    const locgResults = await api.get(`/api/search/locg?q=${encodeURIComponent(q)}`);
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    if (locgResults.length) {
      const arcs = (await cvP).filter(r => r.kind === 'arc');
      _renderWizardResults([...arcs, ...locgResults], q); return;
    }

    // LOCG empty — show the full ComicVine result (arcs + collected-edition volumes).
    const cvResults = await cvP;
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    _renderWizardResults(cvResults, q);
  } catch (err) {
    console.error('wizardSearch error:', err);
    _wizardLastQuery = '';   // let a retry through — this query never landed
    if (seq !== _wizardSeq) return;
    const el = document.getElementById('wizard-results');
    if (el) el.innerHTML = `<div class="state-msg" style="padding:16px 0;font-size:11px;color:var(--amb)">Search failed: ${esc(String(err))}</div>`;
  }
}

function wizardPickSeries(idx) {
  const r = _wizardResults[idx];
  if (!r) return;
  const isArc = r.kind === 'arc';
  _wizardState = { idx, metronId: r.source === 'metron' ? r.id : null, source: isArc ? 'arc' : (r.source || 'metron'), locgId: r.source === 'locg' ? r.id : null, cvArcId: isArc ? r.cv_arc_id : null };
  // An arc owns no folder (lens model): it tracks a cross-title reading order and
  // grabs the collected edition into its main series. So no folder field, and the
  // pull-list line means 'find the collected edition', not 'download every issue'.
  const folderBlock = isArc ? `
    <div class="wizard-arc-note">◆ Story arc — Kometa tracks the reading order across every participating title. It owns no folder; the collected edition lands in its main series' Trades.</div>`
    : `
    <div class="step-label" style="margin-top:16px;margin-bottom:6px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--tq)">Folder path <span style="color:var(--tq);font-weight:400;text-transform:none">(auto-detected — edit if needed)</span></div>
    <div style="display:flex;gap:6px;align-items:center">
      <input class="search-input" id="wizard-folder" placeholder="Resolving…" style="flex:1;margin:0">
      <button class="btn btn-ghost btn-sm" onclick="wizardBrowseFolder()">Browse</button>
    </div>
    <div id="wizard-folder-hint" style="margin-top:6px;font-size:10px;color:var(--tq)">&nbsp;</div>`;
  document.getElementById('modal').innerHTML = `
    <div class="modal-title">Add ${isArc ? 'Story Arc' : 'Series'}</div>
    <div class="wizard-series-preview">
      <img class="wizard-result-thumb" src="${r.source === 'locg' ? esc(r.cover || '') : `/api/metron/series/${r.id}/thumbnail`}" alt="" onerror="this.style.opacity=0">
      <div class="wizard-result-text">
        <div class="wizard-result-title">${esc(r.series || r.name || '')}${isArc ? ' <span class="locg-badge">◆ ARC</span>' : ''}</div>
        <div class="wizard-result-meta">${isArc ? 'Story arc' : esc(r.publisher?.name || '')}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}</div>
      </div>
    </div>
    ${folderBlock}
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px;cursor:pointer;user-select:none">
      <input type="checkbox" id="wizard-pull" checked style="accent-color:var(--pri);width:14px;height:14px">
      <span style="font-family:var(--font);font-size:10px;color:var(--tp)">Add to Pull List</span>
      <span style="font-size:10px;color:var(--tq)">— ${isArc ? "find &amp; grab this storyline's collected edition" : 'queue download of all missing issues now'}</span>
    </label>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="showAddWizard()">← Back</button>
      <button class="btn btn-primary" id="wizard-add-btn" onclick="wizardConfirm()">${isArc ? 'Track Arc' : 'Track Series'}</button>
    </div>
  `;
  if (!isArc) _previewFolder(r);
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
      hint.style.color = res.exists ? 'var(--pri)' : 'var(--tq)';
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
  const { metronId, source, locgId, cvArcId } = _wizardState;
  if (!metronId && !locgId && !cvArcId) return;
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
    if (source === 'arc') {
      payload.cv_arc_id = cvArcId;
      payload.title = r.series || r.name || '';
      payload.publisher_name = r.publisher?.name || '';
      payload.year_began = r.year_began || null;
    } else if (source === 'locg') {
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


// --- tile download tracking ---
// The old ↓ button showed '✓' the moment a job was QUEUED and then went mute —
// rate limits, not-founds, and failures were invisible unless you happened to
// visit Activity. Now the outcome lands where you clicked: the tile button
// tracks the queue state live, terminal states toast, and a finished download
// repaints its tile as owned in place.
const _tilePolls = new Set();   // "seriesId:number" keys with an active poller

function _dlBtn(seriesId, number) {
  // any live download button for this issue — issue tile or pull-list row
  return document.querySelector(`[data-dl="${seriesId}:${number}"]`);
}

function _trackTileDownload(seriesId, number, title) {
  const key = `${seriesId}:${number}`;
  if (_tilePolls.has(key)) return;
  _tilePolls.add(key);

  const tick = async () => {
    let qs;
    try { qs = await api.get(`/api/series/${seriesId}/issues/${number}/queue-status`); }
    catch { _tilePolls.delete(key); return; }

    const onView = currentView === 'series-detail' && currentParams.id === seriesId;
    const btn = _dlBtn(seriesId, number);
    const st = qs.state;

    if (st && !['done', 'failed', 'not_found'].includes(st)) {
      if (btn) {
        const pct = qs.progress?.total ? Math.round(qs.progress.done / qs.progress.total * 100) : null;
        btn.textContent = (st === 'downloading' && pct != null) ? String(pct) : '…';
        btn.title = QUEUE_STATE_LABEL[st] || st;
        btn.disabled = true;
      }
      setTimeout(tick, 2500);
      return;
    }

    _tilePolls.delete(key);
    _updateActivityBadge();   // terminal state — badge reflects it immediately
    const label = `${title ? title + ' ' : ''}#${fmtNum(number)}`;
    if (st === 'done') {
      showToast(`${label} downloaded ✓`);
      const obj = _detailSeries?.issues?.find(i => i.number === number);
      if (obj) obj.owned = 1;
      const tile = onView ? document.querySelector(`.issue-tile[data-num="${number}"]`) : null;
      if (tile && obj && _detailSeries) tile.outerHTML = _issueTileHtml(_detailSeries, obj);
    } else if (st === 'failed' || st === 'not_found') {
      const why = qs.error ? ` — ${qs.error}` : '';
      showToast(`${label}: ${st === 'failed' ? 'failed' : 'not found'}${why}`, 'error');
      if (btn) { btn.textContent = '↓'; btn.title = 'Search for this issue'; btn.disabled = false; }
    }
    // st === null → job left the queue (cleared/parked away); stop quietly
  };
  setTimeout(tick, 2000);
}

async function searchIssue(seriesId, issueNumber, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await api.post(`/api/series/${seriesId}/issues/${issueNumber}/search`, {});
    showToast(`#${fmtNum(issueNumber)} queued`);
    _trackTileDownload(seriesId, issueNumber, _detailSeries?.title || '');
    _updateActivityBadge();
  } catch {
    showToast('Could not queue download', 'error');
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
  const lt = _localToday(), ut = _usToday();
  if (e.owned) return `<span class="pull-status pull-status-owned">✓</span>`;
  if (e.store_date > lt) return `<span class="pull-status pull-status-upcoming">${fmtDayDate(e.store_date)}</span>`;
  if (e.store_date >= ut) return `<span class="pull-status pull-status-today">Today</span>`;
  return `<span class="pull-status pull-status-missing">Missing</span>`;
}

let _pullShowPast = false;
let _pullItems = [];

async function pullDownload(seriesId, number, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await api.post(`/api/series/${seriesId}/issues/${number}/search`, {});
    const item = _pullItems.find(i => i.id === seriesId && i.number === number);
    showToast(`#${fmtNum(number)} queued`);
    _trackTileDownload(seriesId, number, item?.title || '');
    _updateActivityBadge();
  } catch {
    showToast('Could not queue download', 'error');
    btn.textContent = '↓'; btn.disabled = false;
  }
}

async function renderPullList() {
  setTopbar(`
    <button class="btn btn-sm ${_pullShowPast ? 'btn-primary' : 'btn-ghost'}"
      onclick="_togglePullPast(this)">+ Past 4 Weeks</button>
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
  _pullItems = items;

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
              <span class="pull-act">${(!e.owned && e.store_date < _usToday())
                ? `<button class="pull-dl" data-dl="${sid}:${e.number}" title="Download this issue"
                     onclick="event.stopPropagation(); pullDownload(${sid}, ${e.number}, this)">↓</button>`
                : ''}</span>
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
let _activityPollTimer = null;
// After a manual "Search now", the item sits in `queued` while the backend
// thread runs the search async. `queued` isn't an "active" state, so the normal
// poll loop would park and never catch the queued→searching→done/not_found
// transition — the click looks dead. This timestamp keeps the poll loop alive
// for a short window so we actually follow the search to its grave.
let _activityPumpUntil = 0;

// --- activity nav badge ---
// "What happened since you last looked": counts terminal outcomes NEWER than
// your last Activity visit. Worst state wins — red = failed, amber = not
// found, green = completed. Visiting Activity acknowledges everything.
let _badgeTimer = null;

function _actSeen() { return localStorage.getItem('actSeenAt') || ''; }

function _applyBadge(queue) {
  const el = document.getElementById('activity-badge');
  if (!el) return;
  const seen = _actSeen();
  const fresh = q => (q.updated_at || '') > seen;
  const failed = queue.filter(q => q.state === 'failed' && fresh(q)).length;
  const warn   = queue.filter(q => q.state === 'not_found' && fresh(q)).length;
  const done   = queue.filter(q => q.state === 'done' && fresh(q)).length;
  const [n, cls] = failed ? [failed, 'red'] : warn ? [warn, 'amber'] : done ? [done, 'green'] : [0, ''];
  el.textContent = n || '';
  el.className = `nav-badge${n ? ' ' + cls : ''}`;
}

function _ackActivity(queue) {
  // High-water mark from the server's own timestamps — no client-clock games
  const maxTs = queue.reduce((m, q) => (q.updated_at || '') > m ? q.updated_at : m, _actSeen());
  if (maxTs) localStorage.setItem('actSeenAt', maxTs);
  _applyBadge(queue);   // recompute → badge clears
}

async function _updateActivityBadge() {
  try { _applyBadge(await api.get('/api/queue')); } catch {}
}

function _startBadgePolling() {
  clearInterval(_badgeTimer);
  _updateActivityBadge();
  _badgeTimer = setInterval(_updateActivityBadge, 25000);
}

function _fmtBytes(n) {
  if (!n) return '';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

let _activitySig = null;
let _activityPrevStates = null;  // id → state from the last build; drives per-item fades
let _activityRemoving = false;   // true while a row/card is animating out

async function renderActivity() {
  clearTimeout(_activityPollTimer);
  _activitySig = null;          // force a full rebuild when entering the view
  _activityPrevStates = null;   // fresh entry = no per-item fades on first paint
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
  // A removal animation is mid-flight — don't rebuild the list out from under it
  // (a full rebuild here is the flash we're trying to kill). The remover re-polls
  // when it's finished collapsing.
  if (_activityRemoving) return;
  const queue = await api.get('/api/queue');
  _ackActivity(queue);   // you're looking at it — acknowledge + clear the badge
  // Only the progress %/bytes change between polls; the items + their states usually
  // don't. Rebuilding the whole list every 2s recreated every <img>, which reloaded
  // and replayed the cover-in (blur/fade) animation → the cover "flashed". So: rebuild
  // ONLY when the item set or a state changes; otherwise just patch the bars in place.
  const sig = queue.map(q => `${q.id}:${q.state}`).join('|');
  if (sig === _activitySig) {
    queue.forEach(q => {
      const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
      const fill = document.getElementById(`actfill-${q.id}`);
      const text = document.getElementById(`acttext-${q.id}`);
      if (fill) fill.style.width = `${pct}%`;
      if (text) {
        const detail = q.state === 'pending_usenet'
          ? ' · Usenet'
          : q.state === 'pending_torrent'
          ? ' · Torrent' + (q.search_status ? ' · ' + q.search_status : '')
          : (q.progress ? ' — ' + _fmtBytes(q.progress.done) + ' / ' + _fmtBytes(q.progress.total) : '');
        text.textContent = `${pct}%${detail}`;
      }
      const ss = document.getElementById(`actsearch-${q.id}`);
      if (ss && q.search_status) ss.textContent = q.search_status;
    });
  } else {
    const firstBuild = _activitySig === null;
    _activitySig = sig;
    const newStates = {};
    queue.forEach(q => { newStates[q.id] = q.state; });
    const prev = _activityPrevStates;
    _activityPrevStates = newStates;
    const changed = (!firstBuild && prev) ? queue.filter(q => prev[q.id] !== q.state).map(q => q.id) : [];
    const removed = (!firstBuild && prev) ? Object.keys(prev).filter(id => !(id in newStates)) : [];
    if ((changed.length || removed.length) && document.querySelector('.act-wrap')) {
      // Only the items whose state actually changed get the fade — everything
      // else rebuilds in place without so much as a blink. Fading the whole
      // list punished 20 innocent rows for one row's state change.
      _activityRemoving = true;   // hold off re-entrant polls mid-fade
      for (const id of [...changed, ...removed]) {
        const el = document.querySelector(`[data-qid="${id}"]`);
        if (el) { el.style.transition = 'opacity .18s ease'; el.style.opacity = '0'; }
      }
      setTimeout(() => {
        _buildActivityHtml(queue);
        for (const id of changed) {
          const el = document.querySelector(`[data-qid="${id}"]`);
          if (el) {
            el.style.opacity = '0';
            el.style.transition = 'opacity .3s ease';
            requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = '1'; }));
          }
        }
        _activityRemoving = false;
      }, 190);
    } else {
      _buildActivityHtml(queue);
    }
  }
  const hasActive = queue.some(q => ['searching','found','downloading','processing','pending_usenet','pending_torrent'].includes(q.state));
  // Within the post-retry window, keep polling while anything's still queued so
  // we don't freeze on the stale `queued` card and miss the real outcome.
  const pumping = Date.now() < _activityPumpUntil && queue.some(q => q.state === 'queued');
  if (hasActive || pumping) _activityPollTimer = setTimeout(_refreshActivity, 2000);
}

function _actChip(state) {
  const map = {
    queued:      ['chip chip-muted',   'Queued'],
    searching:   ['chip chip-active',  'Searching'],
    found:       ['chip chip-muted',   'Found'],
    downloading: ['chip chip-active',  'Downloading'],
    pending_usenet: ['chip chip-active', 'Usenet'],
    pending_torrent: ['chip chip-active', 'Torrent'],
    processing:  ['chip chip-muted',   'Processing'],
    done:        ['chip chip-done',    'Done'],
    not_found:   ['chip chip-warn',    'Not Found'],
    failed:      ['chip chip-fail',    'Failed'],
  };
  const [cls, label] = map[state] || ['chip chip-muted', state];
  return `<span class="${cls}">${label}</span>`;
}

// Activity rows are kind-agnostic: a trade shows "Vol N" and the series cover
// (no per-issue thumbnail), an issue shows "#N" and its own.
function _actLabel(q) {
  if (q.kind === 'trade') {
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    if (m.vol_range) return `Vol ${m.vol_range[0]}–${m.vol_range[1]}`;
    if (m.vol != null) return `Vol ${m.vol}`;
    return 'Trade';
  }
  return `#${fmtNum(q.issue_number)}`;
}
function _actThumb(q) {
  if (!q.tracked_series_id) return '';
  const seriesThumb = `/api/series/${q.tracked_series_id}/thumbnail`;
  if (q.kind === 'trade') {
    // The trade's own LOCG cover (stashed in meta), falling back to the series.
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return `<img src="${esc(m.cover || seriesThumb)}" onerror="this.src='${seriesThumb}'">`;
  }
  return `<img src="/api/series/${q.tracked_series_id}/issues/${q.issue_number}/thumbnail" onerror="this.src='${seriesThumb}'">`;
}

// Plain-language failure reason for an Activity row — surfaces what used to be a
// hover-only tooltip and softens the dev-speak. Empty for done / no-error rows.
function _actReason(q) {
  if (q.state === 'done' || !q.error) return '';
  const e = q.error;
  const strip = s => s.replace(/^(Usenet|GetComics|Torrent):\s*/i, '');
  if (/\bpages\b.*(collection|webtoon)/i.test(e))     return strip(e);               // already clear + specific
  if (/is #\d+, expected|ComicInfo reports/i.test(e)) return strip(e);               // wrong issue — keep the numbers
  if (/No result on GetComics/i.test(e))              return 'Not on GetComics, usenet or torrents yet';
  if (/stalled, no seeders/i.test(e))                 return 'Torrent had no seeders';
  if (/rate limit.*(retries|giving up)/i.test(e))     return 'Gave up after repeated rate-limits';
  if (/rate limited/i.test(e))                        return 'Rate-limited — will retry automatically';
  if (/RAR.*verify|failed to verify/i.test(e))        return 'Usenet release was incomplete or corrupt';
  if (/already exists|duplicate/i.test(e))            return 'Looked like a duplicate you already have';
  if (/No folder set/i.test(e))                       return 'No comics folder set for this series';
  return strip(e);                                                                   // fall back to the raw reason
}

function _buildActivityHtml(queue) {
  const inProgress = queue.filter(q => ['queued','searching','found','downloading','pending_usenet','pending_torrent','processing'].includes(q.state));
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
      const numStr = _actLabel(q);
      const thumb = _actThumb(q);
      const isDownloading = q.state === 'downloading' || q.state === 'pending_usenet' || q.state === 'pending_torrent';
      const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
      // Usenet progress is a percentage from SAB (no byte counts); GetComics has bytes.
      const detail = q.state === 'pending_usenet'
        ? ' · Usenet'
        : q.state === 'pending_torrent'
        ? ' · Torrent' + (q.search_status ? ' · ' + esc(q.search_status) : '')
        : (q.progress ? ' — ' + _fmtBytes(q.progress.done) + ' / ' + _fmtBytes(q.progress.total) : '');
      const progress = q.state === 'searching' ? `
        <div class="act-card-progress">
          <div class="act-progress-text" id="actsearch-${q.id}">${esc(q.search_status || 'Searching…')}</div>
        </div>` : isDownloading ? `
        <div class="act-card-progress">
          <div class="act-progress-track"><div class="act-progress-fill" id="actfill-${q.id}" style="width:${pct}%"></div></div>
          <div class="act-progress-text" id="acttext-${q.id}">${pct}%${detail}</div>
        </div>` : '';
      const errTip = q.error ? ` title="${esc(q.error)}"` : '';
      const nav = q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
      // Queued items can stall on a retry_after backoff (dupe guard). Give the
      // user the wheel: kick a search right now, or yank it from the queue.
      const actions = q.state === 'queued' ? `
            <button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)" title="Search GetComics/Usenet now (skip backoff)">Search now</button>
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from queue">✕</button>` : '';
      return `
        <div class="act-card${isDownloading ? '' : ' compact'}" data-qid="${q.id}"${errTip}>
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
      const numStr = _actLabel(q);
      const thumb = _actThumb(q);
      const isDone = q.state === 'done';
      const reason = _actReason(q);
      const errTip = q.error ? ` title="${esc(q.error)}"` : '';
      const nav = q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
      const retry = !isDone ? `<button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)">Retry</button>` : '';
      return `
        <div class="act-row${isDone ? ' done' : ''}" data-qid="${q.id}"${errTip}>
          <div class="act-row-cover">${thumb}</div>
          <div class="act-row-meta"${nav}>
            <div class="act-row-title">${esc(q.title)}</div>
            <div class="act-row-issue">${numStr}</div>
            ${reason ? `<div class="act-row-reason">${esc(reason)}</div>` : ''}
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

// Fade + collapse an element out, THEN remove it from the DOM — no instant yank,
// no full-list rebuild (rebuilding is what flashed every cover). Returns a promise
// that resolves once the node is gone. Under reduced-motion we skip the show and bail.
function _animateRowOut(el) {
  return new Promise(resolve => {
    if (!el) { resolve(); return; }
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) { el.remove(); resolve(); return; }
    // Pin the current height so max-height has something to collapse FROM.
    el.style.maxHeight = `${el.offsetHeight}px`;
    // Force a reflow so the starting max-height sticks before we transition.
    void el.offsetHeight;
    el.classList.add('act-removing');
    let done = false;
    const finish = () => { if (done) return; done = true; el.remove(); resolve(); };
    el.addEventListener('transitionend', finish, { once: true });
    // Belt-and-suspenders: if transitionend never fires, fall back to the timeout.
    setTimeout(finish, 360);
  });
}

async function clearHistory(btn) {
  btn.disabled = true; btn.textContent = 'Clearing…';
  await api.post('/api/queue/clear-history', {});
  // Fade the completed rows out in place — don't rebuild the whole list (the in-progress
  // section would flash). Stagger them a touch for a tidy cascade, then drop the sig so
  // the next poll reconciles cleanly.
  _activityRemoving = true;
  const rows = Array.from(document.querySelectorAll('.act-row'));
  rows.forEach((row, i) => { row.style.transitionDelay = `${i * 30}ms`; });
  await Promise.all(rows.map(_animateRowOut));
  // The Completed section header is now stranded with no rows — yank it too.
  document.querySelectorAll('.act-section').forEach(sec => {
    if (!sec.querySelector('.act-row, .act-card')) sec.remove();
  });
  _activitySig = null;
  _activityRemoving = false;
  btn.disabled = false; btn.textContent = 'Clear History';
  _refreshActivity();
}

async function retryQueue(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Searching…';
  await api.post(`/api/queue/${id}/retry`, {});
  // Backend runs the search on a thread — item stays `queued` for a beat, then
  // flips to searching → done/not_found. Pump the poll loop for ~30s so the UI
  // actually rides that transition out instead of going dead on the click.
  _activityPumpUntil = Date.now() + 30000;
  _activitySig = null;   // force a rebuild on the next poll even if state lags
  _refreshActivity();
}

async function removeQueue(id, btn) {
  btn.disabled = true;
  await api.del(`/api/queue/${id}`);
  // Animate ONLY this row/card out — don't rebuild the list (that flashes every cover).
  // Drop the sig so the next poll rebuilds from scratch without trying to re-add the
  // row we just collapsed.
  const el = btn.closest('.act-row, .act-card');
  _activitySig = null;
  _activityRemoving = true;
  await _animateRowOut(el);
  // If that emptied a section (header left dangling), clear the stragglers.
  document.querySelectorAll('.act-section').forEach(sec => {
    if (!sec.querySelector('.act-row, .act-card')) sec.remove();
  });
  _activityRemoving = false;
  if (!document.querySelector('.act-row, .act-card')) _refreshActivity();
}

// --- Settings ---

async function renderSettings() {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const cfg = await api.get('/api/config');
  const komgaCfg  = !!(cfg.komga_url && cfg.komga_user);
  const metronCfg = !!cfg.metron_user;

  setApp(`
    <div class="page-title">Settings</div>
    <div class="settings-grid">
      <div>
        <div class="settings-card">
          ${_settingsHeader('Comics Library', 'required', 'library')}
          <div class="settings-field">
            <label class="settings-field-label" for="f-comics-root">Library path</label>
            <div style="display:flex;gap:6px;align-items:center">
              <input class="settings-input" id="f-comics-root" value="${esc(cfg.comics_root || '')}"
                data-last="${esc(cfg.comics_root || '')}" placeholder="/comics"
                autocomplete="off" spellcheck="false" style="flex:1;margin:0"
                onchange="_settingsChanged(this)">
              <button class="btn btn-ghost btn-sm" onclick="browseSettingsRoot()">Browse</button>
            </div>
          </div>
          <div class="settings-help" id="root-status"></div>
        </div>
        <div class="settings-card" style="margin-top:36px">
          ${_settingsHeader('Sync Schedule', '', 'schedule')}
          ${_settingsField('f-sync-hours', 'Hours (24h, comma-separated)', cfg.sync_hours)}
        </div>
        <div class="settings-card" style="margin-top:36px">
          ${_settingsHeader('Komga', 'optional', 'komga', true, komgaCfg)}
          ${_settingsField('f-komga-url', 'Server URL', cfg.komga_url)}
          ${_settingsField('f-komga-user', 'Username', cfg.komga_user)}
          ${_settingsField('f-komga-pass', 'Password', '', { set: komgaCfg })}
          ${_settingsField('f-komga-lib', 'Library ID', cfg.komga_library_id)}
        </div>
      </div>
      <div>
        <div class="settings-card">
          ${_settingsHeader('Metron', 'optional', 'metron', true, metronCfg)}
          ${_settingsField('f-metron-user', 'Username', cfg.metron_user)}
          ${_settingsField('f-metron-pass', 'Password', '', { set: metronCfg })}
        </div>
        <div class="settings-card" style="margin-top:36px">
          ${_settingsHeader('League of Comic Geeks', 'optional', 'locg', true, cfg.locg_configured)}
          ${_settingsField('f-locg-user', 'Username', cfg.locg_user)}
          ${_settingsField('f-locg-pass', 'Password', '', { set: cfg.locg_configured, ph: 'Enter password' })}
        </div>
        <div class="settings-card" style="margin-top:36px">
          ${_settingsHeader('SABnzbd', 'optional — Usenet downloads', 'sabnzbd', true, cfg.sab_configured)}
          ${_settingsField('f-sab-url', 'Server URL', cfg.sab_url, { ph: 'http://host:8080' })}
          ${_settingsField('f-sab-apikey', 'API Key', '', { set: cfg.sab_configured, ph: 'Enter API key' })}
          <div id="indexers-section"></div>
        </div>
      </div>
    </div>
  `);
  _renderIndexers(cfg.newznab_indexers);
  _updateRootStatus(cfg.comics_root_ok);
}

// Autosave architecture: every field persists itself on change — no Save
// button to forget, no dirty form to lose to a stray navigation. Secrets save
// on blur and never round-trip back into the DOM.
// input id → config key, owning card (for the 'saved' whisper), and which
// integration to re-verify after the value changes.
const _SETTINGS_FIELDS = {
  'f-comics-root': { card: 'library',   key: 'comics_root' },
  'f-komga-url':   { card: 'komga',     key: 'komga_url',        test: 'komga' },
  'f-komga-user':  { card: 'komga',     key: 'komga_user',       test: 'komga' },
  'f-komga-pass':  { card: 'komga',     key: 'komga_pass',       test: 'komga', secret: true },
  'f-komga-lib':   { card: 'komga',     key: 'komga_library_id' },
  'f-metron-user': { card: 'metron',    key: 'metron_user',      test: 'metron' },
  'f-metron-pass': { card: 'metron',    key: 'metron_pass',      test: 'metron', secret: true },
  'f-locg-user':   { card: 'locg',      key: 'locg_user',        test: 'locg' },
  'f-locg-pass':   { card: 'locg',      key: 'locg_pass',        test: 'locg', secret: true },
  'f-sync-hours':  { card: 'schedule',  key: 'sync_hours' },
  'f-sab-url':     { card: 'sabnzbd',   key: 'sab_url',          test: 'sabnzbd' },
  'f-sab-apikey':  { card: 'sabnzbd',   key: 'sab_apikey',       test: 'sabnzbd', secret: true },
};

const _TEST_ENDPOINTS = { komga: 'komga', metron: 'metron', locg: 'locg', sabnzbd: 'sab' };

function _settingsField(id, label, value, opts = {}) {
  const f = _SETTINGS_FIELDS[id] || {};
  const secret = !!f.secret;
  const ph = secret
    ? (opts.set ? 'Leave blank to keep current' : (opts.ph || ''))
    : (opts.ph || '');
  return `
    <div class="settings-field">
      <label class="settings-field-label" for="${id}">${label}</label>
      <input class="settings-input" id="${id}"
        type="${secret ? 'password' : 'text'}"
        value="${esc(value || '')}" data-last="${esc(value || '')}"
        placeholder="${esc(ph)}"
        autocomplete="${secret ? 'new-password' : 'off'}" spellcheck="false"
        onchange="_settingsChanged(this)">
    </div>`;
}

function _settingsHeader(title, tag, cardId, integ = false, configured = false) {
  return `
    <div class="settings-card-header">
      <span>${title}${tag ? ` <span class="settings-opt">${tag}</span>` : ''}</span>
      <span class="settings-head-right">
        <span class="settings-whisper" id="sw-${cardId}"></span>
        ${integ ? `
          <span class="settings-dot ${configured ? 'cfg' : ''}" id="dot-${cardId}"
            title="${configured ? 'configured — not yet verified' : 'not configured'}">${configured ? '○' : ''}</span>
          <button class="btn-link" onclick="testIntegration('${cardId}')">test</button>
          <button class="btn-link" id="dc-${cardId}" style="${configured ? '' : 'display:none'}"
            onclick="disconnectIntegration('${cardId}', this)">disconnect</button>` : ''}
      </span>
    </div>`;
}

let _whisperTimers = {};
function _whisper(cardId, msg, err = false) {
  const el = document.getElementById(`sw-${cardId}`);
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('err', err);
  clearTimeout(_whisperTimers[cardId]);
  _whisperTimers[cardId] = setTimeout(() => { el.textContent = ''; }, err ? 4000 : 1800);
}

function _validSyncHours(v) {
  return /^\d{1,2}(\s*,\s*\d{1,2})*$/.test(v) && v.split(',').every(h => +h >= 0 && +h <= 23);
}

async function _settingsChanged(el) {
  const f = _SETTINGS_FIELDS[el.id];
  if (!f) return;
  const val = el.value.trim();
  if (f.secret && !val) return;                      // blank secret = keep current
  if (!f.secret && val === el.dataset.last) return;  // nothing actually changed

  if (f.key === 'sync_hours' && !_validSyncHours(val)) {
    el.classList.add('input-bad');
    _whisper(f.card, 'hours are 0–23, comma-separated', true);
    return;
  }
  el.classList.remove('input-bad');

  try {
    const cfg = await api.patch('/api/config', { [f.key]: val });
    el.dataset.last = val;
    if (f.secret) { el.value = ''; el.placeholder = 'Leave blank to keep current'; }
    _whisper(f.card, 'saved');
    if (f.key === 'comics_root') _updateRootStatus(cfg.comics_root_ok);
    if (f.test) testIntegration(f.test);             // re-verify after cred change
  } catch (e) {
    _whisper(f.card, 'save failed', true);
    showToast(`Save failed: ${e.message || f.key}`, 'error');
  }
}

function _updateRootStatus(ok) {
  const el = document.getElementById('root-status');
  if (!el) return;
  el.textContent = ok ? '✓ path exists and is writable' : '✗ path missing or read-only';
  el.className = `settings-help ${ok ? 'ok' : 'bad'}`;
}

async function testIntegration(integ) {
  const dot = document.getElementById(`dot-${integ}`);
  if (dot) { dot.textContent = '◌'; dot.className = 'settings-dot cfg'; dot.title = 'testing…'; }
  try {
    const res = await api.post(`/api/test/${_TEST_ENDPOINTS[integ]}`, {});
    if (res.ok) {
      if (dot) { dot.textContent = '●'; dot.className = 'settings-dot ok'; dot.title = 'verified'; }
      const dc = document.getElementById(`dc-${integ}`);
      if (dc) dc.style.display = '';
    } else {
      if (dot) { dot.textContent = '✗'; dot.className = 'settings-dot bad'; dot.title = res.error || 'failed'; }
      showToast(`${integ}: ${res.error || 'connection failed'}`, 'error');
    }
  } catch (e) {
    if (dot) { dot.textContent = '✗'; dot.className = 'settings-dot bad'; dot.title = 'failed'; }
    showToast(`${integ}: test failed`, 'error');
  }
}

async function disconnectIntegration(integ, btn) {
  if (btn.dataset.armed !== '1') {       // two-click confirm — destructive, but no alert() boxes
    btn.dataset.armed = '1';
    btn.textContent = 'sure?';
    setTimeout(() => { btn.dataset.armed = ''; btn.textContent = 'disconnect'; }, 3000);
    return;
  }
  try {
    await api.post(`/api/config/disconnect/${integ}`, {});
    showToast(`${integ} disconnected`);
    renderSettings();
  } catch (e) {
    showToast(`Disconnect failed: ${e.message || integ}`, 'error');
  }
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
    <div class="settings-field-label" style="margin-top:16px">Newznab Indexers</div>
    ${rows}
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <input class="settings-input" id="ix-name" placeholder="Name" autocomplete="off" spellcheck="false" style="flex:1;min-width:70px">
      <input class="settings-input" id="ix-host" placeholder="api.example.info" autocomplete="off" spellcheck="false" style="flex:2;min-width:130px">
      <input class="settings-input" id="ix-apikey" type="password" autocomplete="new-password" placeholder="API key" style="flex:1;min-width:70px">
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
  if (!name || !host || !apikey) { showToast('Name, host, and API key are all required.', 'error'); return; }
  btn.disabled = true; btn.textContent = 'Adding…'; btn.classList.add('btn-working');
  try {
    await api.post('/api/config/indexers', { name, host, apikey, ssl });
    const cfg = await api.get('/api/config');
    _renderIndexers(cfg.newznab_indexers);  // re-renders cleared inputs too
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Add Indexer'; btn.classList.remove('btn-working');
    showToast(`Add failed: ${e.message || 'unknown error'}`, 'error');
  }
}

async function removeIndexer(idx) {
  try {
    await api.del(`/api/config/indexers/${idx}`);
    const cfg = await api.get('/api/config');
    _renderIndexers(cfg.newznab_indexers);
  } catch (e) {
    showToast(`Remove failed: ${e.message || 'unknown error'}`, 'error');
  }
}

// --- Modal ---

function showModal(html) {
  const modal = document.getElementById('modal');
  modal.innerHTML = html;
  modal.classList.remove('hidden');
  document.getElementById('modal-backdrop').classList.remove('hidden', 'closing');
  const first = modal.querySelector('button, input, [tabindex]');
  if (first) setTimeout(() => first.focus(), 30);
}

function closeModal() {
  const modal = document.getElementById('modal');
  const backdrop = document.getElementById('modal-backdrop');
  if (modal.classList.contains('hidden') || backdrop.classList.contains('closing')) return;
  clearTimeout(_issueModalPollTimer);
  document.getElementById('variant-lightbox')?.classList.add('hidden');  // ensure lightbox closes with the modal
  backdrop.classList.add('closing');          // plays scrim-out + modal-out
  setTimeout(() => {
    modal.classList.add('hidden');
    modal.classList.remove('modal-wide');
    backdrop.classList.add('hidden');
    backdrop.classList.remove('closing');
  }, 200);                                     // match exit duration
}

// --- Issue Detail Modal ---

let _issueVariantCovers  = [];
let _issueVariantSelected = new Set();
let _issueVariantPrimary  = null;
let _issueVariantFetched  = false;
let _issueVariantSeriesId = null;
let _issueVariantNumber   = null;

// Tween the issue modal's height around a DOM change so async content (details,
// variants) eases in instead of popping. Measures before/after, animates between.
function _animateModalHeight(mutate) {
  const modal = document.getElementById('modal');
  if (!modal || modal.classList.contains('hidden')) { mutate(); return; }
  const from = modal.getBoundingClientRect().height;
  mutate();
  const to = modal.getBoundingClientRect().height;
  if (from === to || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  // Duration scales with the size of the change: a small grow (details) stays snappy,
  // a big grow (variants grid) eases over more time so it doesn't feel like a snap.
  const dur = Math.min(0.55, Math.max(0.22, Math.abs(to - from) / 850));
  modal.style.height = from + 'px';
  void modal.offsetHeight;                              // commit start height
  modal.style.transition = `height ${dur}s var(--ease-out)`;
  modal.style.height = to + 'px';
  const done = (e) => {
    if (e.propertyName !== 'height') return;
    modal.style.height = '';                            // release back to natural height
    modal.style.transition = '';
    modal.removeEventListener('transitionend', done);
  };
  modal.addEventListener('transitionend', done);
}

function _renderIssueDetails(desc, credits) {
  const el = document.getElementById('issue-modal-details');
  if (!el) return;
  let html = '';
  if (desc) html += `<div class="issue-modal-desc">${esc(desc)}</div>`;
  if (credits && credits.length) {
    const grouped = {};
    for (const c of credits) {
      if (c.name) (grouped[c.role || 'Other'] = grouped[c.role || 'Other'] || []).push(c.name);
    }
    html += '<div class="issue-modal-credits">' +
      Object.entries(grouped).map(([role, names]) =>
        `<div class="issue-modal-credit-row">
          <div class="issue-modal-credit-role">${esc(role)}</div>
          <div class="issue-modal-credit-name">${names.map(esc).join(', ')}</div>
        </div>`).join('') + '</div>';
  }
  _animateModalHeight(() => {
    el.innerHTML = html || '<div class="state-msg" style="font-size:11px;padding:8px 0;color:var(--tq)">No details available.</div>';
    el.style.animation = 'none'; void el.offsetWidth;   // restart the fade
    el.style.animation = 'fade-in var(--t-slow) var(--ease-out)';
  });
}

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

  // variant_cover = your saved variant pick (only present on not-yet-downloaded
  // issues); show it so an upcoming issue reflects the cover you chose. Owned issues
  // carry no pref (it's baked into the CBZ already) and fall through to Komga.
  const imgSrc = issue.variant_cover
    ? issue.variant_cover
    : issue.komga_book_id
      ? `/api/book/${esc(issue.komga_book_id)}/thumbnail`
      : (_metronArt(issue)
          // same server-side fallback chain the grid tiles use — never show the
          // no-cover void when LOCG has variant art for an artless upcoming issue
          || `/api/series/${seriesId}/issues/${issue.number}/thumbnail`);

  const chipMap = {
    owned:   `<span class="chip chip-complete">Owned</span>`,
    missing: `<span class="chip chip-missing">Missing</span>`,
    upcoming:`<span class="chip chip-upcoming">Upcoming</span>`,
    today:   `<span class="chip chip-today">Today</span>`,
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
    } else if (st === 'today') {
      dateHtml = `<div class="issue-modal-date">${fmtDate}</div>
                  <div class="issue-modal-days">Out today</div>`;
    } else {
      dateHtml = `<div class="issue-modal-release-label">Released ${fmtDate}</div>`;
    }
  }

  let footerAction = '';
  if (st === 'owned' && issue.komga_book_id && _appConfig.komga_url) {
    const readerUrl = `${komgaBase()}/book/${esc(issue.komga_book_id)}/read`;
    footerAction = `<a class="btn btn-primary" href="${readerUrl}" target="_blank" rel="noopener">Open in Komga</a>`;
  } else if (st === 'missing' || st === 'today') {
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
            Variants
          </button>
        </div>` : ''}
        <div class="issue-modal-panel active" id="impanel-details">
          <div class="issue-modal-details" id="issue-modal-details">
            ${(issue.metron_issue_id || hasLocgId) ? '<div class="state-msg" style="font-size:11px;padding:8px 0">Loading details…</div>' : ''}
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
    <div class="modal-footer" id="issue-modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${footerAction}
    </div>
  `);

  // Owned but no Komga book id? The id is stamped lazily server-side (the
  // thumbnail route's self-heal, or Komga just finished scanning a fresh
  // download) — often AFTER this page's issue list was fetched, so the cached
  // copy is one render behind. Refetch once and patch the reader link in
  // place instead of making the user reload to get what they already own.
  if (st === 'owned' && !issue.komga_book_id && _appConfig.komga_url) {
    api.get(`/api/series/${seriesId}`).then(fresh => {
      const fi = fresh.issues?.find(i => i.number === number);
      if (!fi?.komga_book_id) return;
      issue.komga_book_id = fi.komga_book_id;   // heal the cached copy too
      if (_issueVariantSeriesId !== seriesId || _issueVariantNumber !== number) return;
      const footer = document.getElementById('issue-modal-footer');
      if (footer && !footer.querySelector('.komga-read-link')) {
        const a = document.createElement('a');
        a.className = 'btn btn-primary komga-read-link';
        a.href = `${komgaBase()}/book/${fi.komga_book_id}/read`;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'Open in Komga';
        footer.appendChild(a);
      }
    }).catch(() => {});
  }

  // Fetch issue details async — Metron when configured, else LOCG (keyless).
  // Both normalise to flat [{role, name}] credits for _renderIssueDetails.
  if (issue.metron_issue_id) {
    try {
      const d = await api.get(`/api/series/${seriesId}/issues/${number}/metron`);
      const credits = (d.credits || []).map(c => ({ role: c.role?.name || 'Other', name: c.creator?.name || '' }));
      _renderIssueDetails(d.desc, credits);
    } catch { _renderIssueDetails('', []); }
  } else if (hasLocgId) {
    try {
      const d = await api.get(`/api/series/${seriesId}/issues/${number}/locg-details`);
      _renderIssueDetails(d.desc, d.credits || []);   // LOCG credits already {role, name}
    } catch { _renderIssueDetails('', []); }
  }

  // Fetch variants in background
  if (hasLocgId) _imFetchVariants(seriesId, number);

  if (st === 'missing' || st === 'today') _pollIssueQueue(seriesId, number);
}

function _imSwitchTab(name) {
  _animateModalHeight(() => {
    ['details', 'variants'].forEach(t => {
      document.getElementById(`imtab-${t}`)?.classList.toggle('active', t === name);
      document.getElementById(`impanel-${t}`)?.classList.toggle('active', t === name);
    });
  });
}

async function _imFetchVariants(seriesId, number) {
  try {
    const data = await api.get(`/api/series/${seriesId}/issues/${number}/variants`);
    if (seriesId !== _issueVariantSeriesId || number !== _issueVariantNumber) return;
    _issueVariantCovers  = data.covers || [];
    // Rehydrate the previous pick so reopening reflects it (★ + included), instead
    // of resetting to blank.
    if (data.selected_ids && data.selected_ids.length) {
      _issueVariantSelected = new Set(data.selected_ids);
      _issueVariantPrimary  = data.primary_id || data.selected_ids[0];
    }
    _issueVariantFetched = true;
    _imRenderVariants();
  } catch(e) {
    const el = document.getElementById('variant-area');
    if (el) el.innerHTML = `<div class="variant-empty">Could not load variants: ${esc(e.message)}</div>`;
  }
}

function _imRenderVariants() {
  const area = document.getElementById('variant-area');
  if (!area) return;
  if (!_issueVariantCovers.length) {
    _animateModalHeight(() => { area.innerHTML = '<div class="variant-empty">No variants found.</div>'; });
    return;
  }
  _animateModalHeight(() => {
  area.className = '';
  area.innerHTML = `<div class="variant-grid">${
    _issueVariantCovers.map((c, i) => `
      <div class="v-card" id="vc-${c.id}" style="animation-delay:${i*STAGGER_MS}ms" onclick="_imToggleVariant('${c.id}')">
        <div class="v-cover">
          <img src="${esc(c.thumb)}" alt="${esc(c.name)}" loading="lazy"
            onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
          <div class="no-img" style="display:none">No image</div>
          <button class="v-star" onclick="_imSetPrimary(event,'${c.id}')" title="Set as cover">★</button>
          <button class="v-mag" onclick="_imOpenLightbox('${c.id}',event)" title="View larger"><svg viewBox="0 0 24 24" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/></svg></button>
        </div>
        <div class="v-name">${esc(c.name)}</div>
      </div>`).join('')
  }</div>`;
  const footer = document.getElementById('variant-footer');
  if (footer) footer.style.display = 'flex';
  _imRefreshCards();   // apply any rehydrated ★/included state to the new cards
  _imUpdateHint();
  });
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
  if (e) e.stopPropagation();   // null when called from the lightbox — no card click to contain
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

// --- Variant lightbox (click ⌕ on a card → big preview, browse + select from here) ---
let _lbIndex = 0;

function _imOpenLightbox(id, e) {
  if (e) e.stopPropagation();
  _lbIndex = _issueVariantCovers.findIndex(c => c.id === id);
  if (_lbIndex < 0) _lbIndex = 0;
  _lbRender();
  document.getElementById('variant-lightbox').classList.remove('hidden');
}
function _lbClose(e) {
  if (e) e.stopPropagation();
  document.getElementById('variant-lightbox').classList.add('hidden');
}
function _lbStep(e, d) {
  if (e) e.stopPropagation();
  const n = _issueVariantCovers.length;
  if (!n) return;
  _lbIndex = (_lbIndex + d + n) % n;
  _lbRender();
}
function _lbRender() {              // image changed — replay the cover-in reveal
  const c = _issueVariantCovers[_lbIndex];
  if (!c) return;
  const img = document.getElementById('vlb-img');
  img.classList.remove('loaded');  // reset so the global cover-in animation replays on load
  img.src = c.large || c.thumb;
  img.alt = c.name || '';
  document.getElementById('vlb-name').textContent = c.name || '';
  document.getElementById('vlb-count').textContent = `${_lbIndex + 1} / ${_issueVariantCovers.length}`;
  _lbControls();
}
function _lbControls() {            // button state only — no image re-animation (avoids the pulse)
  const c = _issueVariantCovers[_lbIndex];
  if (!c) return;
  const inc = document.getElementById('vlb-include');
  const on = _issueVariantSelected.has(c.id);
  inc.classList.toggle('on', on);
  inc.textContent = on ? '✓ Included' : 'Include';
  document.getElementById('vlb-star').classList.toggle('on', _issueVariantPrimary === c.id);
}

// The two buttons index.html had been pointing at since the lightbox shipped.
// They never existed — every click was a silent ReferenceError while the buttons
// sat there looking employable. Both just drive the card-grid state machine, so
// the grid, hint, and Apply button behind the lightbox stay in sync for free.
function _lbToggleInclude() {
  const c = _issueVariantCovers[_lbIndex];
  if (!c) return;
  _imToggleVariant(c.id);
  _lbControls();
}
function _lbSetCover() {
  const c = _issueVariantCovers[_lbIndex];
  if (!c) return;
  // The ★ in the lightbox promised "Set as cover" and then quietly did nothing but
  // stage a pick — you still had to go hunt the Apply button. No more. Include it,
  // crown it primary, and commit right here. _imApplyVariants persists (inject for
  // owned / save pref for upcoming), crossfades the tile, and closes modal+lightbox.
  _issueVariantSelected.add(c.id);
  _issueVariantPrimary = c.id;
  _imRefreshCards();
  _imUpdateHint();
  _lbControls();
  const issue = _detailSeries?.issues?.find(i => i.number === _issueVariantNumber);
  const isOwned = issue ? issueStatus(issue) === 'owned' : false;
  _imApplyVariants(_issueVariantSeriesId, _issueVariantNumber, isOwned);
}

async function _imApplyVariants(seriesId, number, isOwned) {
  if (!_issueVariantSelected.size) return;
  const btn = document.getElementById('variant-apply-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = isOwned ? 'Building…' : 'Saving…';
    btn.classList.add('btn-working');   // spinner — rebuilds take 2-10s, look alive
  }
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
    // Reflect the pick immediately: update the cached issue + crossfade just the
    // changed tile (no full-grid repaint, no hard snap). Works for owned + upcoming —
    // both now carry variant_cover as the display cover.
    const prim = selected.find(c => c.id === _issueVariantPrimary);
    const newSrc = prim ? (prim.large || prim.thumb) : null;
    const obj = _detailSeries?.issues?.find(i => i.number === number);
    if (obj) obj.variant_cover = newSrc;
    if (newSrc) _crossfadeTileCover(number, newSrc);
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Apply'; btn.classList.remove('btn-working'); }
    showToast(`Error: ${e.message || 'failed'}`, 'error');
  }
}

// Crossfade a single tile's cover to a new image — overlay the new one, let the
// existing cover-in animation (blur→focus+fade) bring it in over the old, then drop
// the old. Inserted right after the old img so any date badge stays on top.
function _crossfadeTileCover(number, newSrc) {
  const wrap = document.querySelector(`.issue-tile[data-num="${number}"] .issue-tile-img`);
  if (!wrap) return;
  const old = wrap.querySelector('img');
  const next = document.createElement('img');
  next.alt = '';
  next.style.position = 'absolute';
  next.style.inset = '0';
  next.addEventListener('load', () => {
    next.classList.add('loaded');                 // fires cover-in (blur+zoom+fade)
    setTimeout(() => { if (old) old.remove(); }, 340);
  }, { once: true });
  next.addEventListener('error', () => next.remove(), { once: true });
  if (old) old.after(next); else wrap.appendChild(next);
  wrap.classList.remove('unknown');
  next.src = newSrc;
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
    btn.style.cssText += ';background:var(--pri);border-color:var(--pri)';
    return;
  }
  btn.disabled = false;
  btn.textContent = state === 'not_found' ? 'Not Found · Retry' : 'Failed · Retry';
  btn.onclick = () => issueDownload(seriesId, number);
}

async function issueDownload(seriesId, number) {
  const btn = document.getElementById('issue-dl-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Queuing…'; btn.classList.add('btn-working'); }
  try {
    await api.post(`/api/series/${seriesId}/issues/${number}/search`, {});
    _pollIssueQueue(seriesId, number);
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Download'; btn.classList.remove('btn-working'); }
  }
}

document.addEventListener('keydown', e => {
  // Lightbox is on top — it gets Esc / arrows first.
  if (!document.getElementById('variant-lightbox').classList.contains('hidden')) {
    if (e.key === 'Escape') _lbClose();
    else if (e.key === 'ArrowLeft') _lbStep(e, -1);
    else if (e.key === 'ArrowRight') _lbStep(e, 1);
    return;
  }
  if (e.key === 'Escape' && !document.getElementById('modal').classList.contains('hidden')) {
    closeModal();
  }
});

// Fade covers in once they load (or fail) instead of popping. Delegated in the
// capture phase because the load/error events don't bubble — covers all imgs
// rendered dynamically.
document.addEventListener('load', e => {
  if (e.target.tagName === 'IMG') e.target.classList.add('loaded');
}, true);
document.addEventListener('error', e => {
  if (e.target.tagName === 'IMG') e.target.classList.add('loaded');
}, true);

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

// --- pull-to-refresh (touch only) ---
// Re-fetches the current view's DATA without resetting filters/search state.
const _PTR_VIEWS = new Set(['library', 'series-detail', 'pull-list', 'activity']);
let _ptrStartY = 0, _ptrPulling = false;

function _ptrRefresh() {
  // The gesture means "make it FRESH", not just "repaint the cache" — where
  // the data is born (library, series detail), a pull also kicks a real sync
  // (LOCG/Komga/folder rescan). Repaint is instant; the sync lands behind it.
  if (currentView === 'library') {
    api.post('/api/sync', {}).catch(() => {});
    showToast('Refreshing — full sync started');
    return _loadBrowsePage();
  }
  if (currentView === 'series-detail') {
    const id = currentParams.id;
    _autoSynced.add(id);          // we're syncing right now — don't double-fire
    syncSeries(id, null);         // background; re-renders again if it changed anything
    showToast('Syncing series…');
    return renderSeriesDetail(id);
  }
  if (currentView === 'pull-list')     return _renderPullListContent();
  if (currentView === 'activity') {
    // fresh = give the not-founds another shot (failed stays manual — per-row Retry)
    return api.post('/api/queue/retry-not-found', {}).then(r => {
      if (r.requeued) showToast(`Re-searching ${r.requeued} not-found issue${r.requeued > 1 ? 's' : ''}`);
      return _refreshActivity();
    }).catch(() => _refreshActivity());
  }
}

// #app is the real scroll container (body is overflow:hidden) — window.scrollY
// is ALWAYS 0 here, so it must never be the "are we at the top?" check.
function _appScrollTop() {
  const el = document.getElementById('app');
  return el ? el.scrollTop : 0;
}

document.addEventListener('touchstart', e => {
  _ptrPulling = false;
  if (!_PTR_VIEWS.has(currentView) || _appScrollTop() > 0) return;
  const modal = document.getElementById('modal');
  const lb = document.getElementById('variant-lightbox');
  if (modal && !modal.classList.contains('hidden')) return;
  if (lb && !lb.classList.contains('hidden')) return;
  _ptrStartY = e.touches[0].clientY;
  _ptrPulling = true;
}, { passive: true });

// NOT passive, on purpose: iOS converts an un-prevented downward pull into its
// native rubber-band scroll and fires touchcancel instead of touchend — the
// release handler never runs and the gesture silently dies (the iPad bug).
// preventDefault while actively pulling at the top keeps the gesture ours;
// normal scrolling is untouched because we only prevent when pulling down
// with the scroller already at 0.
document.addEventListener('touchmove', e => {
  if (!_ptrPulling) return;
  const el = document.getElementById('ptr');
  if (!el) return;
  const dist = e.touches[0].clientY - _ptrStartY;
  if (dist <= 0 || _appScrollTop() > 0) { el.style.transform = ''; el.classList.remove('ready'); return; }
  e.preventDefault();
  const d = Math.min(dist * 0.4, 64);   // rubber-band: drag feels heavier than the finger
  el.style.transform = `translateY(${d + 40}px)`;
  el.classList.toggle('ready', d >= 56);
}, { passive: false });

function _ptrFinish(fire) {
  if (!_ptrPulling) return;
  _ptrPulling = false;
  const el = document.getElementById('ptr');
  if (!el) return;
  const go = fire && el.classList.contains('ready');
  el.classList.remove('ready');
  el.style.transform = '';
  if (go) _ptrRefresh();
}

document.addEventListener('touchend', () => _ptrFinish(true));
document.addEventListener('touchcancel', () => _ptrFinish(false));

async function boot() {
  // Always land on the library. Komga and Metron are optional integrations,
  // configured in Settings — never a blocking welcome gate. Kometa runs fine
  // with neither: search and track via LOCG, own via folders.
  const cfg = await api.get('/api/config');
  _appConfig = cfg;
  const { view, params } = _parseHash();
  navigate(view, params);
  _startBadgePolling();
}

boot();
