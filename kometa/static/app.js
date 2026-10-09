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
  put(url, body) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  del(url) {
    return fetch(url, { method: 'DELETE' }).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.status === 204 ? null : r.json().catch(() => null);
    });
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
  // Re-clicking the active nav item used to pushState a duplicate entry —
  // Back then needed N presses to visibly move. Same destination replaces.
  if (location.hash === hash) {
    history.replaceState({ view, params }, '', hash);
  } else {
    history.pushState({ view, params }, '', hash);
  }
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
  const navView = (currentView === 'series-detail' || currentView === 'shelf') ? 'library'
    : currentView === 'readlist' ? 'readlists' : currentView;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === navView);
  });
}

function setTopbar() {
  document.getElementById('topbar-title').textContent = '';
  document.getElementById('topbar-chips').innerHTML = '';
  document.getElementById('topbar-actions').innerHTML = '';
  document.getElementById('topbar-sub').innerHTML = '';
}

function setApp(html) {
  document.getElementById('app').innerHTML = html;
}

function renderView() {
  const view = currentView;
  // The reader is a full-screen layer over whatever view you came from — the
  // view underneath stays painted, so Back is instant.
  if (view === 'read') return renderReader(currentParams);
  closeReader();
  const paint = (() => {
    switch (view) {
      case 'library':       return renderLibraryBrowse();
      case 'ondeck':        return renderOnDeck();
      case 'readlists':     return renderReadLists();
      case 'readlist':      return renderReadList(currentParams.id);
      case 'needs-match':   return renderNeedsMatch();
      case 'singles':       return renderSinglesReport();
      case 'series-detail': return renderSeriesDetail(currentParams.id);
      case 'shelf':         return renderShelfSeries(currentParams.id);
      case 'pull-list':     return renderPullList();
      case 'activity':      return renderActivity();
      case 'settings':      return renderSettings();
      default:              setApp('<div class="state-msg">Not found</div>');
    }
  })();
  // None of the view renderers catch their own fetches, so before this a failed
  // GET marooned the app on "Loading..." with no way out but a hard reload.
  // Catch at the ONE dispatch point instead of in five renderers. The view check
  // keeps a slow loser from painting its error over a view you've already left.
  Promise.resolve(paint).catch(e => {
    console.error(e);
    if (currentView === view) {
      setApp(`<div class="state-msg">Couldn't load this view. ${esc(String(e && e.message || e))}<br><br>
        <button class="btn btn-sm" onclick="renderView()">Retry</button></div>`);
    }
  });
}

// --- Helpers ---

function esc(str) {
  if (str == null) return '';
  // &#39; hardens single-quoted ATTRIBUTE positions. It does NOT make strings
  // safe inside inline onclick JS — the HTML parser decodes entities back to
  // raw quotes before the JS engine parses the handler. Never interpolate a
  // free-text string into inline JS; pass ids and read state (see confirmDelete).
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Komga cover URL for an issue. The ?v= is the file's version (mtime:size) — Komga
// keeps a book id when the file behind it is swapped, so the bare URL kept a dead
// coverless #015 alive in every browser for the 30 days we told them to cache it.
// Kometa's own cover route for an owned issue — the file's cover first (validated
// per request), Komga only as a fallback. Asking Komga's thumbnail directly kept
// the OLD file's picture after a re-download until Komga's next scan (2026-10-08).
function bookThumb(issue) {
  return `/api/series/${issue.tracked_series_id}/issues/${issue.number}/thumbnail`;
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
  return String(parseFloat(n));
}

// How a collected edition names itself — tile label, modal title, queue toast,
// Activity row. One place, or "Vol"/"Vols"/format drift across four copies.
function _volLabel(t, fallback) {
  return t.vol_range ? `Vols ${t.vol_range[0]}–${t.vol_range[1]}`
       : t.vol != null ? `Vol ${t.vol}` : (fallback ?? t.format);
}

// The issue/trade modal's left cover column — image (dimmed on load failure)
// or the blank slab. Three modals were carrying identical copies of this.
function _modalCoverHtml(src, alt) {
  return `<div class="issue-modal-cover">
        ${src
          ? `<img src="${esc(src)}" alt="${esc(alt || '')}" onerror="this.style.opacity='0.1'">`
          : `<div class="issue-modal-no-cover"></div>`}
      </div>`;
}

function issueStatus(issue) {
  const lt = _localToday(), ut = _usToday();
  if (issue.owned) return 'owned';
  if (issue.ignored) return 'ignored';              // told to stop chasing it — never 'missing'
  if (!issue.store_date) return 'unknown';
  if (issue.store_date > lt) return 'upcoming';   // future on YOUR calendar
  if (issue.store_date >= ut) return 'today';      // out today (your date) — or still dropping in the US
  return 'missing';                                 // only once it's passed in the US too
}

function fmtDayDate(iso) {
  if (!iso || iso >= '9000') return 'TBD';        // the catalogue's 'unscheduled' placeholder (9999-01-01)
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

// One poll loop per series — a manual Sync + pull-to-refresh + stale auto-sync
// used to stack three concurrent 2s pollers on the same id, each burning a
// fetch per tick for 90s. The backend already one-shots the sync itself; this
// is the client-side twin of that guard.
const _syncInFlight = new Set();

async function syncSeries(id, btn, pre = null, force = false) {
  const _resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = 'Sync'; } };
  if (_syncInFlight.has(id)) { _resetBtn(); return; }
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  _syncInFlight.add(id);
  let before, preSynced;
  try {
    // `pre` = a series payload the caller JUST fetched (renderSeriesDetail's
    // auto-sync) — reuse it instead of an identical back-to-back GET.
    before = pre || await api.get(`/api/series/${id}`);
    preSynced = before.last_synced;
    await api.post(`/api/sync/${id}${force ? '?force=1' : ''}`, {});
  } catch (e) {
    _syncInFlight.delete(id);
    _resetBtn();
    if (btn) showToast('Sync failed — is the server up?');
    console.error(e);
    return;
  }
  const deadline = Date.now() + 90_000;
  const poll = setInterval(async () => {
    try {
      const s = await api.get(`/api/series/${id}`);
      if (s.last_synced !== preSynced || Date.now() > deadline) {
        clearInterval(poll);
        _syncInFlight.delete(id);
        // Only repaint if the user is STILL on this series — and only when the
        // sync changed something visible. The old else-branch here painted a
        // long-dead, router-unreachable series page over WHATEVER view you
        // were on whenever a background sync landed — the infamous
        // "page keeps bouncing to the library by itself" poltergeist.
        if (currentView === 'series-detail' && currentParams.id === id) {
          const changed = !before
            || s.owned !== before.owned || s.missing !== before.missing
            || s.upcoming !== before.upcoming || s.next_release !== before.next_release
            || s.calendar_date !== before.calendar_date
            || (s.issues || []).length !== (before.issues || []).length;
          if (changed) await renderSeriesDetail(id);
        }
        _resetBtn();
      }
    } catch {
      clearInterval(poll);
      _syncInFlight.delete(id);
      _resetBtn();
    }
  }, 2000);
}

// --- Library Browse ---

let browseState = { search: '', searchTimer: null, toggles: { upcoming: false, missing: false, pulling: false }, _cache: null, sortKey: 'date', sortDir: { date: 'asc' } };

async function renderLibraryBrowse() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Library';
  // No Sync All button — the scheduler syncs everything at 5/12/17, stale series
  // self-sync on view, and series detail has its own Sync. The machine does the
  // work; a global button here was a comfort blanket with no feedback.
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-primary btn-sm" onclick="showAddWizard()">+ Add Series</button>
  `;
  browseState.search  = '';
  browseState.toggles = { upcoming: false, missing: false, pulling: false };
  browseState._cache  = null;
  // The sort you chose last time sticks (per browser); nearest release first by default.
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('kometa.librarySort') || 'null'); } catch {}
  browseState.sortKey = saved?.key || 'date';
  browseState.sortDir = saved?.dir || { date: 'asc' };
  setApp('<div class="state-msg">Loading...</div>');
  await _loadBrowsePage();
}

// Default view is everything — no tab to click. Upcoming/Missing are
// independent toggles that narrow it down, not exclusive tabs: both on
// shows the union (anything needing attention), not just series matching
// both at once — see _renderBrowseResults.
// Pull list / Reading are SCOPE filters (narrow, AND); Upcoming / Missing stay
// the needs-attention UNION inside whatever scope is left. Every series is a
// Kometa series — "pull list" is the one that means "actively downloading".
const BROWSE_TOGGLES = [
  { key: 'pulling',  label: 'Pull list' },
  { key: 'upcoming', label: 'Upcoming' },
  { key: 'missing',  label: 'Missing' },
  { key: 'favourites', label: '★ Favourites' },
];

const _isReading = s => (s.in_progress ?? 0) > 0 || (s.read_count ?? 0) > 0;

function _browseFilterTabs() {
  return `<div class="browse-filters">
    ${BROWSE_TOGGLES.map(f => `
      <button class="browse-filter-tab u-label${browseState.toggles[f.key] ? ' active' : ''}"
        data-key="${f.key}" onclick="browseFilter('${f.key}')">${f.label}</button>
    `).join('')}
  </div>`;
}

function browseFilter(key) {
  browseState.toggles[key] = !browseState.toggles[key];
  // Tabs render once on first paint (see _loadBrowsePage) and _renderBrowseResults
  // only touches #browse-results — so a toggle click has to update ITS OWN
  // button's active class by hand instead of a full re-render finding it.
  document.querySelectorAll('.browse-filter-tab').forEach(b => {
    if (b.dataset.key === key) b.classList.toggle('active', browseState.toggles[key]);
  });
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
  try { localStorage.setItem('kometa.librarySort', JSON.stringify({ key: browseState.sortKey, dir: browseState.sortDir })); } catch {}
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
        <input class="browse-search" id="browse-search" type="search" autocomplete="off" spellcheck="false" placeholder="Search your collection…"
          value="${esc(browseState.search)}"
          oninput="browseSearch(this.value)">
        ${_browseFilterTabs()}
        ${_browseSortControls()}
      </div>
      <div id="browse-results"><div class="state-msg">Loading...</div></div>
    `);
    document.getElementById('browse-search')?.focus();
  }

  // The library is the whole shelf: tracked series (acquisition) + every other
  // series folder (reader-only). Shelf failing must not cost the tracked grid.
  const [tracked, shelf] = await Promise.all([
    api.get('/api/series'),
    api.get('/api/shelf').catch(() => ({ series: [], tracked_reading: {} })),
  ]);
  const tr = shelf.tracked_reading || {};
  browseState._trackedCount = tracked.length;
  // Unmatched series live in their own section (Needs matching) until you've
  // picked the run — the Library is the matched shelf.
  _updateNeedsBadge(tracked.filter(_needsMatch).length);
  browseState._cache = tracked.filter(s => !_needsMatch(s)).map(s => ({ ...s, kind: 'series', ...(tr[s.id] || {}) }))
    .concat((shelf.series || []).map(s => ({ ...s, kind: 'shelf' })));
  _renderBrowseResults();
  // Nothing tracked yet on a configured install? Open the one door they'd open
  // anyway — but give them ~5s to read the empty state and reach for it themselves
  // first. Re-check everything when the timer fires: they may have navigated off,
  // added a series, or opened another modal in the meantime.
  clearTimeout(_autoWizardTimer);
  if (browseState._trackedCount === 0 && _appConfig.comics_root_ok) {
    _autoWizardTimer = setTimeout(() => {
      if (currentView === 'library'
          && (browseState._trackedCount ?? 0) === 0
          && _appConfig.comics_root_ok
          && document.getElementById('modal-backdrop')?.classList.contains('hidden')) {
        showAddWizard();
      }
    }, 5000);
  }
}
let _autoWizardTimer = null;

function _renderBrowseResults() {
  const { toggles, search, sortKey, sortDir, _cache: all } = browseState;
  if (!all) return;

  const q = search.toLowerCase();
  const anyToggleOn = toggles.upcoming || toggles.missing;
  let filtered = all.filter(s => {
    if (q && !s.title.toLowerCase().includes(q)) return false;
    if (toggles.pulling && !(s.kind === 'series' && s.on_pull_list)) return false;
    if (toggles.favourites && !s.favourite) return false;
    // Neither toggle on -> no narrowing (the default, everything). Either on ->
    // UNION: "needs attention" (upcoming release OR missing issue), not the
    // (much rarer, and less useful) intersection of both at once.
    if (!anyToggleOn) return true;
    // Missing = gaps you've ASKED for. A run you own 12/40 of and aren't
    // pulling isn't missing anything — it's just a partial run.
    return (toggles.upcoming && (s.upcoming ?? 0) > 0)
        || (toggles.missing && s.on_pull_list && (s.missing ?? 0) > 0);
  });

  if (sortKey === 'alpha') {
    const dir = sortDir.alpha === 'asc' ? 1 : -1;
    filtered = filtered.slice().sort((a, b) => dir * a.title.localeCompare(b.title));
  } else if (sortKey === 'date') {
    const dir = (sortDir.date ?? 'asc') === 'asc' ? 1 : -1;
    // calendar_date, not next_release: this week's release holds its slot even
    // once it's downloaded (next_release skips owned issues — a fresh #1 used to
    // sink to the bottom the moment it landed).
    filtered = filtered.slice().sort((a, b) => {
      if (!a.calendar_date && !b.calendar_date) return 0;
      if (!a.calendar_date) return 1;
      if (!b.calendar_date) return -1;
      return dir * a.calendar_date.localeCompare(b.calendar_date);
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
    const pub   = s.publisher ? `<div class="series-card-publisher u-truncate">${esc(s.publisher.toUpperCase())}</div>` : '';
    if (s.kind === 'shelf') return _shelfCardHtml(s, i, pub);
    // Release-day issues not yet down count toward the total and turn it amber —
    // 145/146, not a '145/145 complete' while #146 is out today.
    const gap   = (s.missing ?? 0) + (s.out_today ?? 0);
    const total = (s.owned ?? 0) + gap;
    const pct   = total ? Math.round((s.owned / total) * 100) : 0;
    // Amber only when you're pulling it: a gap is "missing" because you asked for
    // the series. Not pulling = grey count, still honest about what you own.
    const color = gap > 0 ? (s.on_pull_list ? 'var(--amb)' : 'var(--tq)')
                          : (total > 0 ? 'var(--pri)' : 'var(--tq)');
    const nextRelease = s.calendar_date
      ? `<div class="series-card-next-release">${_fmtReleaseDate(s.calendar_date)}</div>` : '';
    const thumbSrc  = s.card_image || `/api/series/${s.id}/thumbnail`;
    const thumbFall = s.card_image  ? `this.src='/api/series/${s.id}/thumbnail'` : `this.style.opacity='0.15'`;
    return `
      <div class="series-card card-cascade" style="animation-delay:${Math.min(i,14)*STAGGER_MS}ms" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="${esc(thumbSrc)}" alt="${esc(s.title)}"
            loading="lazy" onerror="${thumbFall}">
          ${nextRelease}${s.favourite ? '<div class="series-card-fav" title="Favourite">★</div>' : ''}
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

// An untracked series: on the shelf, readable, nothing to fetch — so no release
// badge and no owned/missing bar. The count is books (or books read, once you've
// started), in the quiet colour.
function _shelfCardHtml(s, i, pub) {
  const go = `navigate('shelf', {id: ${s.id}})`;
  const count = _isReading(s) ? `${s.read_count}/${s.book_count} read` : `${s.book_count}`;
  const pct = _isReading(s) ? Math.round((s.read_count / s.book_count) * 100) : 0;
  return `
      <div class="series-card card-cascade" style="animation-delay:${Math.min(i,14)*STAGGER_MS}ms" tabindex="0" role="button"
        onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="/api/shelf/${s.id}/cover" alt="${esc(s.title)}"
            loading="lazy" onerror="this.style.opacity='0.15'">
        </div>
        <div class="series-card-bar-track">
          <div class="series-card-bar-fill" style="width:${pct}%;background:var(--tq)"></div>
        </div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(s.title)}</div>
          <div class="series-card-count" style="color:var(--tq)">${count}</div>
        </div>
        ${pub}
      </div>`;
}

// --- On Deck: where reading happens -------------------------------------------
// Three rows from the reading tables (kometa/ondeck.py): what you're in the
// middle of, what's next in series you've finished something of, and what's
// coming for series you're caught up on. Suggestions and reading lists later.
async function renderOnDeck() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'On Deck';
  document.getElementById('topbar-actions').innerHTML = '';
  setApp('<div class="state-msg">Loading...</div>');
  const d = await api.get('/api/ondeck');
  if (currentView !== 'ondeck') return;
  const pct = c => c.page_count && c.progress ? Math.round(c.progress.page / c.page_count * 100) : 0;
  const read = c => `navigate('read', {book: ${c.book_id}})`;
  const series = c => c.series_id ? `navigate('series-detail', {id: ${c.series_id}})` : (c.shelf_id ? `navigate('shelf', {id: ${c.shelf_id}})` : '');
  const bookCard = (c, tag, dismissable) => `
    <div class="issue-tile od-card" id="od-${c.book_id}" role="button" tabindex="0" title="${esc(c.series)} ${esc(c.label)}" onclick="${read(c)}" onkeydown="if(event.key==='Enter'||event.key===' ')${read(c)}">
      <div class="issue-tile-img"><img src="/api/books/${c.book_id}/cover" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        ${tag ? `<span class="od-tag">${esc(tag)}</span>` : ''}
        <button class="od-menu" title="Actions" aria-label="Actions" onclick="event.stopPropagation(); _bookActions(${JSON.stringify({ book_id: c.book_id, series: c.series, label: c.label, number: c.number ?? null, series_id: c.series_id || null, shelf_id: c.shelf_id || null, dismissable: !!dismissable, completed: !!(c.progress && c.progress.completed), page_count: c.page_count || null }).replace(/"/g, '&quot;')})">⋯</button>
        ${c.progress ? `<div class="od-bar"><div style="width:${pct(c)}%"></div></div>` : ''}</div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>`;
  const soonCard = c => `
    <div class="issue-tile od-card" role="button" tabindex="0" title="${esc(c.series)} ${esc(c.label)}" onclick="${series(c)}" onkeydown="if(event.key==='Enter'||event.key===' ')${series(c)}">
      <div class="issue-tile-img"><img src="/api/series/${c.series_id}/issues/${c.number}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        <span class="od-tag${c.status === 'not_owned' ? ' amber' : ''}">${esc(c.status_label)}</span></div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>`;
  const favCard = c => c.kind === 'series' ? `
    <div class="issue-tile od-card" role="button" tabindex="0" title="${esc(c.series)}" onclick="navigate('series-detail', {id: ${c.series_id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail', {id: ${c.series_id}})">
      <div class="issue-tile-img"><img src="/api/series/${c.series_id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"><span class="od-tag">★</span></div>
      <div class="issue-tile-num">${esc(c.series)}</div>
    </div>` : bookCard(c, '★');
  const row = (title, help, cards, empty) => `
    <div class="od-row">
      <div class="od-head"><span class="series-card-title">${title}</span></div>
      ${cards.length ? `<div class="issue-grid od-grid">${cards.join('')}</div>` : `<div class="od-empty">${empty}</div>`}
    </div>`;
  setApp(
    row('Continue reading', 'where you left off, most recent first', d.continue.map(c => bookCard(c, null, true)),
        'Nothing in progress. Open anything in the Library and it shows up here.') +
    row('Next', 'the next unread issue in series you’ve finished something of', d.next.map(c => bookCard(c, 'Next')),
        'Finish an issue and its series lands here.') +
    row('Coming soon', 'series you’re caught up on whose next issue isn’t here yet', d.soon.map(soonCard),
        'Nothing waiting. Either you’re not caught up on anything, or everything you’re caught up on has nothing coming.') +
    row('Recently released', 'newest release dates you own, one card per series', (d.released || []).map(c => bookCard(c)),
        'Nothing with a release date yet.') +
    row('Recently added', 'newest files on the shelf, one card per series', (d.added || []).map(c => bookCard(c)),
        'Nothing new on the shelf.') +
    ((d.favourites || []).length ? row('Favourites', 'what you starred', d.favourites.map(favCard), '') : '')
  );
  // what the task rows already show never repeats in a discovery row
  const onPage = [...new Set([...d.continue, ...d.next, ...d.soon, ...(d.released || []), ...(d.added || [])].map(c => c.series_id).filter(Boolean))];
  _loadBecause(onPage);
  _loadTrending();
}

// --- Trending (kometa/trending.py): what shops sold most, from ICv2 ----------------
function _trendCard(e) {
  const owned = e.owned && e.series_id;
  const go = owned ? `navigate('series-detail', {id: ${e.series_id}})`
    : `showCatalogueModal(${JSON.stringify({ title: e.metron_title || e.series, publisher: e.publisher, year: e.year, cover: e.cover, metron_series_id: e.metron_series_id, rank: e.rank, why: [e.number != null ? `#${fmtNum(e.number)} charted` : 'charted'] }).replace(/"/g, '&quot;')})`;
  const label = e.number != null ? `#${fmtNum(e.number)}` : (e.vol != null ? `Vol ${e.vol}` : '');
  const action = owned ? ''
    : e.metron_series_id
      ? `<button class="od-menu rel-get" title="Track this series (pull list off)" onclick="event.stopPropagation(); _relTrack(${JSON.stringify({ metron_id: e.metron_series_id, title: e.metron_title || e.series, publisher_name: e.publisher || '', year_began: e.year || null, on_pull_list: false }).replace(/"/g, '&quot;')}, this)">TRACK</button>`
      : `<button class="od-menu rel-get" title="Find it in the catalogue" onclick="event.stopPropagation(); showAddWizard(${JSON.stringify(e.series).replace(/"/g, '&quot;')})">FIND</button>`;
  return `<div class="series-card rel-card${owned ? '' : ' rel-gap'}" tabindex="0" role="button" onclick="${go}" title="${esc(e.title)} · ${esc(e.publisher || '')}">
    <div class="series-card-img-wrap">${e.cover ? `<img class="series-card-cover" src="${esc(e.cover)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
      <div class="series-card-next-release trend-rank">#${e.rank}</div>${action}</div>
    <div class="series-card-footer"><div class="series-card-title">${esc(e.series)}</div>
      <div class="series-card-count" style="color:${owned ? (e.have_issue ? 'var(--pri)' : 'var(--amb)') : 'var(--tq)'}">${owned ? (e.have_issue ? 'have it' : label || 'on shelf') : label}</div></div>
    <div class="series-card-publisher u-truncate">${esc(e.publisher || '')}</div>
  </div>`;
}

async function _loadTrending(attempt = 0) {
  let d;
  try { d = await api.get('/api/trending'); } catch { return; }
  if (currentView !== 'ondeck') return;
  const app = document.getElementById('app');
  app.querySelector('.trend-row')?.remove();
  if (!d.comics.length && !d.graphic_novels.length) return;
  if (d.pending && attempt < 4) setTimeout(() => _loadTrending(attempt + 1), 5000);
  const month = d.month ? ` · ${esc(d.month)}` : '';
  app.insertAdjacentHTML('beforeend', `<div class="od-row trend-row">
    <div class="od-head"><span class="series-card-title">Trending</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">top sellers in comic shops${month} · ICv2</span></div>
    <div class="series-grid">${d.comics.slice(0, 24).map(_trendCard).join('')}</div>
    ${d.graphic_novels.length ? `<div class="od-head" style="margin-top:14px"><span class="series-card-title">Trending collected editions</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">top graphic novels${month} · ICv2</span></div>
      <div class="series-grid">${d.graphic_novels.slice(0, 12).map(_trendCard).join('')}</div>` : ''}
  </div>`);
}

// --- Related / Suggestions (kometa/related.py) ----------------------------------
// One card shape for both rows: the series' cover, its title, and WHY it's here
// — a shared creator, a shared arc, or 'on a reading list together'.
function _relCard(r) {
  if (r.kind === 'gap') return _gapCard(r);
  if (r.kind === 'catalogue') return _catalogueCard(r);
  const go = `navigate('series-detail', {id: ${r.series_id}})`;
  const why = (r.why || []).join(' · ');
  const because = r.because && r.because.length ? `because you read ${r.because.map(esc).join(', ')}` : '';
  return `<div class="series-card rel-card" tabindex="0" role="button" onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
    <div class="series-card-img-wrap"><img class="series-card-cover" src="/api/series/${r.series_id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"></div>
    <div class="series-card-footer"><div class="series-card-title">${esc(r.title)}</div>
      <div class="series-card-count" style="color:${r.owned && r.owned >= r.total ? 'var(--pri)' : 'var(--tq)'}">${r.total ? `${r.owned || 0}/${r.total}` : ''}</div></div>
    <div class="series-card-publisher u-truncate rel-why" title="${esc(why)}">${esc(why)}</div>
    ${because ? `<div class="series-card-publisher u-truncate rel-because">${because}</div>` : ''}
  </div>`;
}

// A suggestion you don't have: a reading-list gap, with the list's cover for it
// and Get right on the card (the same Get as the list page).
function _gapCard(r) {
  const go = `navigate('readlist', {id: ${r.list_id}})`;
  const why = (r.why || []).join(' · ');
  return `<div class="series-card rel-card rel-gap" tabindex="0" role="button" onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
    <div class="series-card-img-wrap">${r.cover ? `<img class="series-card-cover" src="${esc(r.cover)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
      <button class="od-menu rel-get" title="Get this" aria-label="Get" onclick="event.stopPropagation(); _rlGet(${r.list_id}, ${r.item_id}, this)">GET</button></div>
    <div class="series-card-footer"><div class="series-card-title">${esc(r.title)}</div>
      <div class="series-card-count" style="color:var(--amb)">not here</div></div>
    <div class="series-card-publisher u-truncate rel-why" title="${esc(why)}">${esc(why)}</div>
  </div>`;
}

// A series you don't have at all, by someone whose work you've been reading:
// the catalogue's cover, and Track puts it on the shelf (pull list off).
function _catalogueCard(r) {
  const why = (r.why || []).join(' · ');
  const payload = JSON.stringify({ metron_id: r.metron_series_id, title: r.title, year_began: r.year || null, on_pull_list: false }).replace(/"/g, '&quot;');
  const open = `showCatalogueModal(${JSON.stringify(r).replace(/"/g, '&quot;')})`;
  return `<div class="series-card rel-card rel-gap" tabindex="0" role="button" title="${esc(r.title)}${r.year ? ' (' + r.year + ')' : ''}" onclick="${open}" onkeydown="if(event.key==='Enter'||event.key===' ')${open}">
    <div class="series-card-img-wrap">${r.cover ? `<img class="series-card-cover" src="${esc(r.cover)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
      <button class="od-menu rel-get" title="Track this series (pull list off)" aria-label="Track" onclick="event.stopPropagation(); _relTrack(${payload}, this)">TRACK</button></div>
    <div class="series-card-footer"><div class="series-card-title">${esc(r.title)}</div>
      <div class="series-card-count" style="color:var(--tq)">${r.year || ''}</div></div>
    <div class="series-card-publisher u-truncate rel-why" title="${esc(why)}">${esc(why)}</div>
  </div>`;
}

async function _relTrack(payload, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const added = await api.post('/api/series', payload);
    showToast(`Tracking ${added.title}`);
    navigate('series-detail', { id: added.id });
  } catch (e) { btn.disabled = false; btn.textContent = 'TRACK'; showToast('Couldn’t track that', 'error'); }
}

async function _loadRelated(id, attempt = 0) {
  let d;
  try { d = await api.get(`/api/series/${id}/related`); } catch { return; }
  _paintRelated(id, d, attempt);
}

function _paintRelated(id, d, attempt = 0) {
  if (currentView !== 'series-detail' || currentParams.id !== id) return;
  // the catalogue is being asked in the background: look again in a few seconds, a few times
  if (d.pending && attempt < 4) setTimeout(() => _loadRelated(id, attempt + 1), 4000);
  const app = document.getElementById('app');
  app.querySelector('.rel-row')?.remove();
  const out = d.outward || [];
  if (!d.related.length && !out.length && !d.pending) return;
  app.insertAdjacentHTML('beforeend', `<div class="od-row rel-row">
    <div class="od-head"><span class="series-card-title">Related</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">on your shelf · same creators · same arc · same reading list</span></div>
    ${d.related.length ? `<div class="series-grid">${d.related.map(_relCard).join('')}</div>`
      : '<div class="od-empty">Nothing on the shelf yet — the catalogue is still being asked about this series.</div>'}
    ${out.length ? `<div class="od-head" style="margin-top:14px"><span class="series-card-title">By the same people</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">not on your shelf · Track puts them there</span></div>
      <div class="series-grid">${out.map(_relCard).join('')}</div>` : ''}
  </div>`);
}

// "Because you read {series}" (kometa/related.py because_rows): one row per
// anchor, the title is the reason, so no card says why. Rows under four items
// don't come back; a series shows once across the page.
async function _loadBecause(exclude, attempt = 0) {
  let d;
  try { d = await api.get(`/api/ondeck/because?exclude=${exclude.join(',')}`); } catch { return; }
  if (currentView !== 'ondeck') return;
  if (d.pending && attempt < 4) setTimeout(() => _loadBecause(exclude, attempt + 1), 4000);
  const app = document.getElementById('app');
  app.querySelectorAll('.sug-row').forEach(e => e.remove());
  const anchor = app.querySelector('.trend-row');
  for (const r of d.rows) {
    const html = `<div class="od-row sug-row">
      <div class="od-head"><span class="series-card-title">Because you read ${esc(r.anchor)}</span>
        <span class="u-label" style="color:var(--tq);margin-left:10px">on the shelf first, then what's near it</span></div>
      <div class="series-grid">${r.items.map(_relCard).join('')}</div>
    </div>`;
    if (anchor) anchor.insertAdjacentHTML('beforebegin', html); else app.insertAdjacentHTML('beforeend', html);
  }
}

// One ⋯ per card, one sheet: Read, the series behind it, Not now, read state.
// Same shape as the reader's own ⋯ — tap the thing, get its actions.
function _bookActions(c) {
  // A tracked series' issue: the series page's own modal, cover and details and
  // all, with the card's actions in its footer. A trade or an untracked shelf
  // book has no issue to look up, so it gets the same modal shape built from
  // the book itself (cover, pages, star, rating) — never a bare list of buttons.
  if (c.series_id && c.number != null) return showIssueModal(c.series_id, c.number, { book: c });
  return showBookModal(c);
}

// The issue modal's shape for a book that isn't an issue: trades, one-shots,
// untracked shelf books. Same cover column, same footer slots.
async function showBookModal(c) {
  let bk = null;
  try { bk = await api.get(`/api/books/${c.book_id}`); } catch {}
  const label = c.label || '';
  const series = c.series || bk?.title || '';
  // a trade's file name already carries the series: don't say it twice
  const title = label && series && label.toLowerCase().startsWith(series.toLowerCase().replace(/\s*\(\d{4}\)$/, '')) ? label : `${series} ${label}`.trim();
  const completed = bk?.progress?.completed ?? c.completed;
  const goSeries = c.series_id ? `detailTab = 'trades'; navigate('series-detail', {id: ${c.series_id}})`
    : c.shelf_id ? `navigate('shelf', {id: ${c.shelf_id}})` : '';
  const pages = bk?.page_count ? `${bk.page_count} pages` : '';
  const prog = bk?.progress && !completed ? ` · on page ${bk.progress.page}` : (completed ? ' · read' : '');
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(`/api/books/${c.book_id}/cover`, label)}
      <div class="issue-modal-info">
        <div class="issue-modal-num">${esc(title)}</div>
        <div class="issue-modal-series">${esc(series)}</div>
        <div class="issue-modal-meta">${esc([bk?.publisher, pages].filter(Boolean).join(' · '))}${esc(prog)}</div>
        <div style="margin:8px 0"><span class="chip chip-complete">On the shelf</span>${c.number == null ? ' <span class="chip chip-collected">Collected edition</span>' : ''}</div>
        ${bk ? _markRowHtml(bk) : ''}
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${goSeries ? `<button class="btn btn-ghost" onclick="closeModal(); ${goSeries}">Go to series</button>` : ''}
      ${c.dismissable ? `<button class="btn btn-ghost" onclick="closeModal(); _odDismiss(${c.book_id}, document.getElementById('od-${c.book_id}')?.querySelector('.od-menu'))">Not now</button>` : ''}
      <button class="btn btn-ghost" onclick="closeModal(); _bookSetRead(${c.book_id}, ${completed ? 'false' : 'true'})">${completed ? 'Mark as unread' : 'Mark as read'}</button>
      <button class="btn btn-primary" onclick="closeModal(); navigate('read', {book: ${c.book_id}})">Read</button>
    </div>`);
}

// A series that isn't on the shelf (Related's "By the same people", Trending):
// the same modal, the catalogue's facts, and Track where Read would be.
function showCatalogueModal(r) {
  const why = (r.why || []).filter(Boolean);
  const payload = JSON.stringify({ metron_id: r.metron_series_id, title: r.metron_title || r.title, publisher_name: r.publisher || '',
    year_began: r.year || null, on_pull_list: false }).replace(/"/g, '&quot;');
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(r.cover || '', r.title)}
      <div class="issue-modal-info">
        <div class="issue-modal-num">${esc(r.title)}</div>
        <div class="issue-modal-series">${esc([r.publisher, r.year].filter(Boolean).join(' · '))}</div>
        <div style="margin:8px 0"><span class="chip" style="color:var(--tq);border-color:var(--tq)">Not on the shelf</span>
          ${r.total ? `<span class="chip chip-neutral">${r.total} issues</span>` : ''}${r.rank ? `<span class="chip chip-neutral">#${r.rank} this month</span>` : ''}</div>
        ${why.length ? `<div class="issue-modal-meta">${why.map(esc).join(' · ')}</div>` : ''}
        <div class="issue-modal-meta" style="margin-top:10px;color:var(--td)">Track adds it to the Library with the pull list off: issues and covers from the catalogue, nothing downloaded until you ask.</div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${r.metron_series_id ? `<button class="btn btn-primary" onclick="closeModal(); _relTrack(${payload}, this)">Track series</button>`
        : `<button class="btn btn-primary" onclick="closeModal(); showAddWizard(${JSON.stringify(r.title).replace(/"/g, '&quot;')})">Find in catalogue</button>`}
    </div>`);
}

async function _bookSetRead(bookId, read) {
  try {
    const b = await api.get(`/api/books/${bookId}`);
    const page = read ? (b.page_count || 1) : 1;
    await api.put(`/api/books/${bookId}/progress`, { page, completed: read, updated_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z') });
    showToast(read ? 'Marked as read' : 'Marked as unread');
    if (currentView === 'ondeck') renderOnDeck();
  } catch (e) { showToast('Couldn’t change that', 'error'); }
}

// 'Not now' on a Continue reading tile: hidden, place kept, back the next time
// you read it. Undo on the toast for a few seconds.
async function _odDismiss(bookId, btn) {
  const tile = document.getElementById(`od-${bookId}`);
  const row = tile && tile.closest('.od-row');
  const html = tile && tile.outerHTML;
  try { await api.post(`/api/books/${bookId}/dismiss`, {}); }
  catch (e) { showToast('Couldn\u2019t hide that', 'error'); return; }
  await _animateTileOut(tile);
  showToastAction('Hidden from Continue reading', 'Undo', async () => {
    try { await api.post(`/api/books/${bookId}/dismiss?undo=1`, {}); } catch { showToast('Couldn\u2019t undo that', 'error'); return; }
    // Back in at the end of the row — no page reload, no flash. The server
    // sorts by last read, which is where it'd land anyway.
    if (currentView !== 'ondeck' || !row || !html || !row.isConnected) { renderOnDeck(); return; }
    let grid = row.querySelector('.od-grid');
    if (!grid) {                                  // the row had emptied out
      row.querySelector('.od-empty')?.remove();
      grid = document.createElement('div'); grid.className = 'issue-grid od-grid'; row.appendChild(grid);
    }
    grid.insertAdjacentHTML('beforeend', html);
    _animateTileIn(grid.lastElementChild);
  });
}

// The reverse of _animateTileOut: a tile arrives small and clear, then settles.
function _animateTileIn(tile) {
  if (!tile) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) return;
  tile.classList.add('od-entering');
  void tile.offsetWidth;
  tile.classList.remove('od-entering');
}

// A tile leaves a grid the way an Activity row leaves a list: it fades and
// shrinks (--t-mid), then the tiles after it slide into the gap (--t-slow,
// --ease-out) instead of jumping — FLIP: measure, remove, measure, play the
// difference backwards. Reduced motion: it's just gone.
function _animateTileOut(tile) {
  return new Promise(resolve => {
    if (!tile) { resolve(); return; }
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const grid = tile.parentElement;
    if (reduce || !grid) { tile.remove(); resolve(); return; }
    const others = [...grid.children].filter(el => el !== tile);
    const before = new Map(others.map(el => [el, el.getBoundingClientRect()]));
    tile.classList.add('od-leaving');
    setTimeout(() => {
      tile.remove();
      let moving = 0;
      for (const el of others) {
        const a = before.get(el), b = el.getBoundingClientRect();
        const dx = a.left - b.left, dy = a.top - b.top;
        if (!dx && !dy) continue;
        moving++;
        el.style.transition = 'none';
        el.style.transform = `translate(${dx}px, ${dy}px)`;
        void el.offsetWidth;
        el.style.transition = 'transform var(--t-slow) var(--ease-out)';
        el.style.transform = '';
        el.addEventListener('transitionend', () => { el.style.transition = ''; }, { once: true });
      }
      setTimeout(resolve, moving ? 300 : 0);
    }, 200);
  });
}

// Shelf clean-up search: hides any row (twin, combine group, collected edition,
// split, unknown) whose text doesn't contain the words; empties a group's
// heading when nothing in it is left.
function _sgFilter(q) {
  q = (q || '').trim().toLowerCase();
  const words = q.split(/\s+/).filter(Boolean);
  document.querySelectorAll('#app .nm-row').forEach(row => {
    const t = row.textContent.toLowerCase();
    row.style.display = words.every(w => t.includes(w)) ? '' : 'none';
  });
  document.querySelectorAll('#app .singles-group').forEach(g => {
    g.style.display = [...g.querySelectorAll('.nm-row')].some(r => r.style.display !== 'none') ? '' : 'none';
  });
}

// --- Needs matching ------------------------------------------------------------
// Its own section, not a Library filter: a folder that hasn't been matched to a
// run isn't on the shelf yet, it's in the in-tray. Pick the run and it moves to
// the Library; decide it's trash and Remove bins the folder too (kometa/trash.py).
const _needsMatch = s => s.match_status === 'needs_match' || s.match_status === 'pending';

function _updateNeedsBadge(n) {
  const el = document.getElementById('needs-badge');
  if (!el) return;
  el.textContent = n || '';
  el.className = 'nav-badge' + (n ? ' amber' : '');
  // Nothing to match → no menu item. It comes back by itself the moment a new
  // folder lands on the shelf (every count refresh passes through here).
  el.closest('.nav-item')?.classList.toggle('nav-hidden', !n);
}

async function renderNeedsMatch() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Needs matching';
  document.getElementById('topbar-actions').innerHTML = '';
  setApp('<div class="state-msg">Loading...</div>');
  const all = await api.get('/api/series');
  const rows = all.filter(_needsMatch).sort((a, b) =>
    (a.match_status === 'needs_match' ? 0 : 1) - (b.match_status === 'needs_match' ? 0 : 1)
    || a.title.localeCompare(b.title));
  _updateNeedsBadge(rows.length);
  if (currentView !== 'needs-match') return;
  if (!rows.length) {
    setApp(`<div class="empty-state"><div class="empty-state-title">Everything's matched</div>
      <div style="margin-top:8px;color:var(--tq);font-size:13px">New folders land here until they're matched to a run.</div></div>`);
    return;
  }
  const waiting = rows.filter(s => s.match_status === 'pending').length;
  _nmRows = rows;
  setApp(`
    <div class="nm-intro">${rows.length} folder${rows.length === 1 ? '' : 's'} not yet matched to a run${waiting
      ? ` — ${waiting} still in the queue` : ''}.
      <a class="btn-link" style="margin-left:10px" onclick="navigate('singles')">Shelf clean-up →</a></div>
    <input class="browse-search nm-search" id="nm-search" type="text" placeholder="Search titles, publishers, folders"
      value="${esc(_nmQuery)}" oninput="_nmFilter(this.value)" autocomplete="off" spellcheck="false">
    <div class="nm-list" id="nm-list">${_nmListHtml()}</div>`);
  if (_nmQuery) document.getElementById('nm-search')?.focus();
}

// Search the in-tray: title, publisher or folder, as you type. Kept across
// re-renders (a match fading out shouldn't wipe what you were looking for).
let _nmRows = [], _nmQuery = '';
function _nmFilter(q) {
  _nmQuery = q;
  const el = document.getElementById('nm-list');
  if (el) el.innerHTML = _nmListHtml();
}
function _nmListHtml() {
  const q = _nmQuery.trim().toLowerCase();
  const rows = q ? _nmRows.filter(s => [s.title, s.publisher, s.folder_path].some(v => (v || '').toLowerCase().includes(q))) : _nmRows;
  if (!rows.length) return `<div class="nm-intro" style="padding:14px 0">Nothing matches "${esc(_nmQuery)}".</div>`;
  return rows.map((s, i) => _needsRowHtml(s, i)).join('');
}

// --- Single-file series report (read-only) ----------------------------------------
// 414 of 876 series are one file. The report says what each one IS — a real
// one-shot, a trade filed as a run, one folder of a split mini, or not yet known
// — so the per-case steps (merge, file under parent) start from facts.
const _SINGLE_KINDS = [
  ['collected', 'Collected editions', 'Trades, deluxe and omnibus editions filed as series. These belong under their parent run as trades.'],
  ['split',     'Split minis',        'One series spread one issue per folder. These belong merged into one folder.'],
  ['unknown',   'Not sure yet',       'Metron type still being fetched, or no catalogue match and a name that says nothing.'],
];

async function renderSinglesReport() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Shelf clean-up';
  document.getElementById('topbar-actions').innerHTML =
    `<button class="btn btn-ghost btn-sm" onclick="renderSinglesReport()">Refresh</button>`;
  setApp('<div class="state-msg">Looking at every folder…</div>');
  const r = await api.get('/api/report/singles');
  if (currentView !== 'singles') return;
  const c = r.counts;
  const open = id => `navigate('series-detail', {id: ${id}})`;
  const row = x => `
    <div class="nm-row" id="sg-${x.id}">
      <img class="nm-cover" src="/api/series/${x.id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'" onclick="${open(x.id)}">
      <div class="nm-main" onclick="${open(x.id)}">
        <div class="nm-title">${esc(x.title)}</div>
        <div class="nm-meta u-truncate">${esc(x.file)}</div>
        <div class="tidy-why">${esc(x.why)}${x.kind !== 'collected' && x.parent_guess ? ` · parent? <b>${esc(x.parent_guess)}</b>` : ''}${x.split_base ? ` · merge as <b>${esc(x.split_base)}</b>` : ''}</div>
        ${x.kind === 'collected' ? `<div class="tidy-why sg-collects" id="sgc-${x.id}">looking up what it collects…</div>` : ''}
      </div>
      <span class="nm-status${x.match_status === 'needs_match' || x.match_status === 'pending' ? ' amber' : ''}">${esc(x.metron_type || (x.match_status === 'needs_match' || x.match_status === 'pending' ? 'unmatched' : ''))}</span>
      <div class="nm-actions" id="sga-${x.id}"></div>
    </div>`;
  setApp(`
    <input class="browse-search nm-search" id="sg-search" type="text" placeholder="Search this page" autocomplete="off" spellcheck="false" oninput="_sgFilter(this.value)">
    <div class="nm-intro">Twin folders, collected editions filed as series, split minis, and single files not yet placed${r.counts.one_shot ? ` (${r.counts.one_shot} one-shots are fine and not shown)` : ''}. Nothing here changes anything until you press Merge or File under on a row.
      ${r.types_pending ? `<br>Metron types known for ${r.types_known}, still fetching ${r.types_pending} (a few seconds each) — refresh in a while and "not sure yet" shrinks.` : ''}</div>
    ${_SINGLE_KINDS.map(([k, label, help]) => {
      const rows = r.rows.filter(x => x.kind === k);
      if (!rows.length) return '';
      if (k === 'split') rows.sort((a, b) => (a.split_base || '').localeCompare(b.split_base || '') || a.title.localeCompare(b.title));
      else rows.sort((a, b) => a.title.localeCompare(b.title));
      return `<div class="singles-group">
        <div class="singles-head"><span class="series-card-title">${label} <span class="settings-opt">${rows.length}</span></span>
          <span class="tidy-why">${help}</span></div>
        <div class="nm-list">${rows.map(row).join('')}</div></div>`;
    }).join('')}`);
  _loadCollections();
  _loadTwins();
  _loadCombine();
}

// Same series, many folders (kometa/combine.py): 'The Adventures of Tintin - X'
// ×25, the One Bad Day one-shots. Combine shows the order and the new names;
// you can reorder, untick extras and rename before anything moves.
async function _loadCombine() {
  let r;
  try { r = await api.get('/api/report/combine'); } catch { return; }
  if (currentView !== 'singles' || !r.groups.length) return;
  const html = `<div class="singles-group" id="combine-group">
    <div class="singles-head"><span class="series-card-title">Same series, many folders <span class="settings-opt">${r.groups.length}</span></span>
      <span class="tidy-why">One album per folder. Combine makes one series, numbered in order, each file keeping its title.</span></div>
    <div class="nm-list">${r.groups.map((g, i) => `
      <div class="nm-row" id="cg-${i}">
        <img class="nm-cover" src="/api/series/${g.ids[0]}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        <div class="nm-main">
          <div class="nm-title">${esc(g.prefix)}</div>
          <div class="nm-meta u-truncate">${g.count} folders${g.unmatched < g.count ? ` (${g.count - g.unmatched} already matched to their own runs)` : ''} · ${esc(g.publisher || '')} · ${esc(g.members.slice(0, 4).map(m => m.subtitle).join(', '))}${g.count > 4 ? '…' : ''}</div>
        </div>
        <div class="nm-actions"><button class="btn btn-primary btn-sm" onclick='_combineOpen(${JSON.stringify(g).replace(/'/g, '&#39;')}, ${i})'>Combine</button></div>
      </div>`).join('')}</div></div>`;
  (document.getElementById('sg-top') || document.getElementById('app')).insertAdjacentHTML('beforeend', html);
}

let _cb = null;   // the combine being edited: {group, title, order: [ids], excluded: Set}
async function _combineOpen(group, rowIndex) {
  _cb = { group, rowIndex, title: group.prefix, order: group.members.map(m => m.id),
          excluded: new Set(group.members.filter(m => m.matched).map(m => m.id)) };   // matched runs stay out unless you say
  await _combineRender();
}

async function _combineRender() {
  const ids = _cb.order.filter(id => !_cb.excluded.has(id));
  let p = null, err = null;
  try { p = await api.post('/api/report/combine/plan', { ids, title: _cb.title, order: ids }); }
  catch (e) { err = e.message || 'plan failed'; }
  const byId = Object.fromEntries(_cb.group.members.map(m => [m.id, m]));
  const row = (id, i) => {
    const m = byId[id], ex = _cb.excluded.has(id), item = p && p.items.find(x => x.id === id);
    return `<div class="tidy-row${ex ? ' tidy-leave' : ''}">
      <span class="tidy-tag u-label">${ex ? 'out' : item ? '#' + String(item.n).padStart(2, '0') : ''}</span>
      <div class="tidy-paths"><div class="u-truncate">${esc(m.subtitle)}${m.year ? ` <span class="tidy-why">(${m.year})</span>` : ''}${m.matched ? ' <span class="tidy-why" style="color:var(--amb)">matched to its own run</span>' : ''}</div>
        ${item ? `<div class="tidy-to u-truncate">${esc(item.to)}</div>` : ''}</div>
      <span style="display:flex;gap:2px;flex:none">
        <button class="btn btn-ghost btn-sm" title="Up" onclick="_combineMove(${id}, -1)" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn btn-ghost btn-sm" title="Down" onclick="_combineMove(${id}, 1)" ${i === _cb.order.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn btn-ghost btn-sm" title="${ex ? 'Include' : 'Leave out'}" onclick="_combineToggle(${id})">${ex ? '+' : '–'}</button>
      </span></div>`;
  };
  showModal(`
    <div class="modal-header"><h2>Combine into one series</h2></div>
    <div class="modal-body tidy-body">
      <div class="settings-field"><label class="settings-field-label u-label">Series name</label>
        <input class="settings-input" id="cb-title" value="${esc(_cb.title)}" onchange="_cb.title = this.value.trim(); _combineRender()"></div>
      <div class="u-label" style="color:var(--tq);margin:8px 0">Order: <a class="btn-link" onclick="_combineSort('year')">by year</a> · <a class="btn-link" onclick="_combineSort('alpha')">alphabetical</a>${p ? ` · ${p.counts.albums} albums${p.counts.duplicates ? `, ${p.counts.duplicates} duplicate file${p.counts.duplicates === 1 ? '' : 's'} binned` : ''}` : ''}</div>
      ${err ? `<div style="color:var(--pink);margin-bottom:8px">${esc(err)}</div>` : ''}
      ${_cb.order.map(row).join('')}
      ${p ? `<div class="tidy-why" style="margin-top:12px">Files move into <b>${esc(p.target.replace(/^\/comics\//, ''))}</b>. The ${_cb.group.count} stub series go; reading progress follows the files. The result is a shelf series with no catalogue run.</div>` : ''}
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="cb-apply" ${p ? '' : 'disabled'}>Combine</button>
    </div>`);
  const b = document.getElementById('cb-apply');
  if (b) b.onclick = async () => {
    b.disabled = true; b.textContent = 'Combining…';
    try {
      const r = await api.post('/api/report/combine/apply', { ids, title: _cb.title, order: ids });
      closeModal();
      const errs = (r.errors || []).length;
      showToast(`${r.title}: ${r.moved} albums combined${r.binned ? `, ${r.binned} duplicates binned` : ''}${errs ? `, ${errs} failed` : ''}`, errs ? 'error' : '');
      const el = document.getElementById(`cg-${_cb.rowIndex}`); if (el) el.remove();
      if (!errs) navigate('series-detail', { id: r.series_id });
    } catch (e) { closeModal(); showToast(`Combine failed: ${e.message || ''}`, 'error'); }
  };
}
function _combineMove(id, d) {
  const i = _cb.order.indexOf(id), j = i + d;
  if (j < 0 || j >= _cb.order.length) return;
  [_cb.order[i], _cb.order[j]] = [_cb.order[j], _cb.order[i]];
  _combineRender();
}
function _combineToggle(id) { _cb.excluded.has(id) ? _cb.excluded.delete(id) : _cb.excluded.add(id); _combineRender(); }
function _combineSort(how) {
  const byId = Object.fromEntries(_cb.group.members.map(m => [m.id, m]));
  _cb.order.sort((a, b) => how === 'alpha'
    ? byId[a].subtitle.localeCompare(byId[b].subtitle)
    : ((byId[a].year || 9999) - (byId[b].year || 9999)) || byId[a].subtitle.localeCompare(byId[b].subtitle));
  _combineRender();
}

// Twin folders: one run, two folders (kometa/twins.py). Rendered at the top of
// the report; Merge shows the plan — moves, duplicates and which copy wins —
// and nothing happens until you confirm.
async function _loadTwins() {
  let r;
  try { r = await api.get('/api/report/twins'); } catch { return; }
  if (currentView !== 'singles' || !r.rows.length) return;
  const html = `<div class="singles-group" id="twins-group">
    <div class="singles-head"><span class="series-card-title">Twin folders <span class="settings-opt">${r.rows.length}</span></span>
      <span class="tidy-why">Two folders for one run. The series' own folder is kept; the twin's files move in, true duplicates keep the better copy.</span></div>
    <div class="nm-list">${r.rows.map(x => `
      <div class="nm-row" id="tw-${x.shelf_id}">
        <img class="nm-cover" src="/api/shelf/${x.shelf_id}/cover" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        <div class="nm-main">
          <div class="nm-title">${esc(x.title)}</div>
          <div class="nm-meta u-truncate">${esc(x.folder.replace(/^\/comics\//, ''))} · ${x.book_count} file${x.book_count === 1 ? '' : 's'}</div>
          <div class="tidy-why">same name as <b>${esc(x.series_title)}</b> · ${esc(x.series_folder.replace(/^\/comics\//, ''))}${x.publisher_differs ? ` · <span style="color:var(--amb)">different publisher (${esc(x.publisher || '?')} vs ${esc(x.series_publisher || '?')}) — check it's the same run</span>` : ''}</div>
        </div>
        <div class="nm-actions"><button class="btn btn-primary btn-sm" onclick="_mergeTwin(${x.shelf_id}, ${x.series_id})">Merge</button></div>
      </div>`).join('')}</div></div>`;
  (document.getElementById('sg-top') || document.getElementById('app')).insertAdjacentHTML('beforeend', html);
}

async function _mergeTwin(shelfId, seriesId) {
  let p;
  try { p = await api.post('/api/report/twins/plan', { shelf_id: shelfId, series_id: seriesId }); }
  catch (e) { showToast(`Merge: ${e.message || 'couldn’t plan'}`, 'error'); return; }
  const c = p.counts;
  const row = (tag, text, sub) => `<div class="tidy-row"><span class="tidy-tag u-label">${tag}</span>
    <div class="tidy-paths"><div class="u-truncate">${esc(text)}</div>${sub ? `<div class="tidy-why">${esc(sub)}</div>` : ''}</div></div>`;
  showModal(`
    <div class="modal-header"><h2>Merge into ${esc(p.series_title)}?</h2></div>
    <div class="modal-body tidy-body">
      <div class="u-label" style="color:var(--tq)">${c.move} file${c.move === 1 ? '' : 's'} move in · ${c.duplicate} duplicate${c.duplicate === 1 ? '' : 's'} (${c.twin_wins} where the twin's copy is better) · the twin folder goes</div>
      ${p.moves.map(m => row('move', m.file)).join('')}
      ${p.as_is.map(a => row('move', a.file, 'no issue number — moved as is')).join('')}
      ${p.duplicates.map(d => row('dupe', d.twin_file, d.keep === 'twin' ? `keeps this; bins the series' ${d.series_file}` : `binned; the series already has ${d.series_file}`)).join('')}
      <div class="tidy-why" style="margin-top:12px">Binned files sit in _trash for 7 days. Reading progress follows the files.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="merge-btn">Merge</button>
    </div>`);
  document.getElementById('merge-btn').onclick = async (ev) => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = 'Merging…';
    try {
      const r = await api.post('/api/report/twins/apply', { shelf_id: shelfId, series_id: seriesId });
      closeModal();
      const errs = (r.errors || []).length;
      showToast(`Merged into ${r.series_title}: ${r.moved} moved, ${r.binned} binned${errs ? `, ${errs} failed` : ''}`, errs ? 'error' : '');
      const el = document.getElementById(`tw-${shelfId}`); if (el) el.remove();
    } catch (e) { closeModal(); showToast(`Merge failed: ${e.message || ''}`, 'error'); }
  };
}

// Collected editions: what each one collects (publisher wiki) and the run on
// your shelf to file it under. Rows fill in as the background lookups land.
let _collectionsPoll = null;
async function _loadCollections() {
  clearTimeout(_collectionsPoll);
  let r;
  try { r = await api.get('/api/report/collections'); } catch { return; }
  if (currentView !== 'singles') return;
  let pending = 0;
  for (const x of r.rows) {
    const line = document.getElementById(`sgc-${x.id}`), acts = document.getElementById(`sga-${x.id}`);
    if (!line) continue;
    const link = x.wiki_url ? ` <a class="btn-link" href="${esc(x.wiki_url)}" target="_blank" rel="noopener">wiki</a>` : '';
    if (x.status === 'pending') { pending++; line.textContent = 'looking up what it collects…'; continue; }
    if (x.status === 'ready') {
      line.innerHTML = `collects <b>${esc(x.collects_summary)}</b>${x.year ? ` · ${x.year}` : ''}${link}`;
      if (acts) acts.innerHTML = `<button class="btn btn-primary btn-sm" onclick="_fileUnder(${x.id}, ${x.parent_id}, null, {stay: true})">File under ${esc(x.parent_title)}</button>`;
    } else if (x.status === 'no_parent') {
      line.innerHTML = `collects <b>${esc(x.collects_summary)}</b>${x.year ? ` · ${x.year}` : ''} — <span style="color:var(--amb)">${esc(x.why)}</span>${link}`;
    } else {
      line.innerHTML = `<span style="color:var(--tq)">${esc(x.why)}</span>`;
    }
  }
  if (pending && r.progress && r.progress.running) _collectionsPoll = setTimeout(_loadCollections, 4000);
}

function _needsRowHtml(s, i) {
  const go = `navigate('series-detail', {id: ${s.id}})`;
  const owned = s.owned ?? 0;
  const status = s.match_status === 'needs_match'
    ? '<span class="nm-status amber">pick the run</span>'
    : '<span class="nm-status">waiting to match</span>';
  return `
    <div class="nm-row card-cascade" id="nm-${s.id}" style="animation-delay:${Math.min(i,14)*STAGGER_MS}ms">
      <img class="nm-cover" src="/api/series/${s.id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"
        onclick="${go}">
      <div class="nm-main" onclick="${go}" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
        <div class="nm-title">${esc(s.title)}</div>
        <div class="nm-meta u-truncate">${s.publisher ? esc(s.publisher) + ' · ' : ''}${owned} file${owned === 1 ? '' : 's'}${s.folder_path ? ' · ' + esc(s.folder_path.replace(/^\/comics\//, '')) : ''}</div>
      </div>
      ${status}
      <div class="nm-actions">
        ${s.match_status === 'pending' ? `<button class="btn btn-ghost btn-sm" onclick="_matchNow(${s.id}, this)">Match now</button>` : ''}
        <button class="btn btn-ghost btn-sm" onclick="${go}">Open</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDelete(${s.id})">Remove</button>
      </div>
    </div>`;
}

// Removing goes through confirmDelete / doDelete — ONE modal, one meaning —
// from the series page, its match banner and a Needs matching row alike.

// --- Tidy: files to the house convention, dry run first --------------------------
// Publisher/Series - Subtitle (Year)/Series - Subtitle #001 (Year).cbz. The modal IS
// the dry run: every rename and conversion listed, every file left alone and why.
// Nothing moves until Apply. Progress survives (paths follow the files in the DB).
async function _tidyPlan(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  let p;
  try { p = await api.get(`/api/series/${id}/tidy`); }
  catch (e) { showToast(`Tidy: ${e.message || 'couldn’t plan'}`, 'error'); if (btn) { btn.disabled = false; btn.textContent = 'Tidy'; } return; }
  if (btn) { btn.disabled = false; btn.textContent = 'Tidy'; }
  const c = p.counts;
  const nothing = !p.ops.length;
  const row = (from, to, tag) => `<div class="tidy-row"><span class="tidy-tag u-label">${tag}</span>
      <div class="tidy-paths"><div class="tidy-from u-truncate">${esc(from)}</div><div class="tidy-to u-truncate">${esc(to)}</div></div></div>`;
  showModal(`
    <div class="modal-header"><h2>Tidy files</h2></div>
    <div class="modal-body tidy-body">
      <div class="u-label" style="color:var(--tq)">${esc(p.run_title)}${p.year ? ` · ${p.year}` : ''} · ${c.rename_folder ? 'folder renamed, ' : ''}${c.rename_file} renamed, ${c.convert} converted to CBZ, ${c.leave} left alone</div>
      ${nothing ? '<div class="state-msg" style="padding:14px 0">Already tidy. Nothing to do.</div>' : ''}
      ${p.ops.filter(o => o.kind === 'rename_folder').map(o => row(o.from.split('/').slice(-2).join('/'), o.to.split('/').slice(-2).join('/'), 'folder')).join('')}
      ${p.ops.filter(o => o.kind !== 'rename_folder').map(o => row(o.from, o.to, o.kind === 'convert' ? 'cbr→cbz' : 'rename')).join('')}
      ${p.leave.length ? `<div class="u-label" style="color:var(--tq);margin-top:14px">Left alone</div>` : ''}
      ${p.leave.map(l => `<div class="tidy-row tidy-leave"><span class="tidy-tag u-label">keep</span>
        <div class="tidy-paths"><div class="u-truncate">${esc(l.file)}</div><div class="tidy-why">${esc(l.reason)}</div></div></div>`).join('')}
      ${p.run_title !== p.title ? `<div class="tidy-why" style="margin-top:12px">The series will be called <b>${esc(p.run_title)}</b> (was ${esc(p.title)}).</div>` : ''}
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">${nothing ? 'Close' : 'Cancel'}</button>
      ${nothing ? '' : `<button class="btn btn-primary" id="tidy-apply-btn">Apply</button>`}
    </div>`);
  const apply = document.getElementById('tidy-apply-btn');
  if (apply) apply.onclick = async (ev) => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = 'Working…'; b.classList.add('btn-working');
    try {
      const r = await api.post(`/api/series/${id}/tidy`, {});
      closeModal();
      const errs = (r.errors || []).length;
      showToast(`Tidied: ${r.rename_file} renamed, ${r.convert} converted${r.rename_folder ? ', folder renamed' : ''}${errs ? ` — ${errs} failed` : ''}`, errs ? 'error' : '');
      renderSeriesDetail(id);
    } catch (e) {
      closeModal(); showToast(`Tidy failed: ${e.message || ''}`, 'error');
    }
  };
}

async function _undoTrash(path) {
  try {
    await api.post('/api/trash/restore', { path });
    showToast('Restored — it’ll be back in a moment');
    setTimeout(() => { if (currentView === 'needs-match') renderNeedsMatch(); }, 1500);
  } catch (e) {
    showToast(`Couldn't restore: ${e.message || ''}`, 'error');
  }
}

// A toast with one action button (Undo). Stays up longer than a plain toast.
function showToastAction(msg, label, fn) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.innerHTML = `${esc(msg)} <button class="btn-link toast-action">${esc(label)}</button>`;
  el.querySelector('.toast-action').onclick = () => { el.className = 'toast-hidden'; fn(); };
  el.className = 'toast-show';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = 'toast-hidden'; }, 8000);
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
let _pendingIssueOpen = null;   // {seriesId, number} — open this issue once its run renders

// FALLBACK ONLY — arc tiles call the real showIssueModal directly now (same
// modal, Download button, variants, everything every series page gets), scoped
// to the row's resolved tracked_series_id. This thin version only fires if that
// resolution somehow comes back empty (a participating run not yet tracked).
function showArcIssueModal(readingOrder) {
  const s = _detailSeries;
  const i = (s?.arc_issues || []).find(x => x.reading_order === readingOrder);
  if (!i) return;
  const owned = !!i.owned;
  const covered = !owned && s.collected;
  const statusChip = owned ? '<span class="chip chip-complete">Owned</span>'
    : covered ? '<span class="chip chip-collected">◆ Covered by a trade</span>'
    : '<span class="chip chip-missing">Missing</span>';
  const num = `#${esc(String(i.number ?? '?'))}`;
  const canReadInKomga = owned && i.komga_book_id && _appConfig.komga_url;
  const detailText = canReadInKomga
    ? `This issue lives in its own run — <b>${esc(i.source_title || '')}</b>.`
    : `This issue lives in its own run — <b>${esc(i.source_title || '')}</b>. Open it there for full
       details, variants and to get it.`;
  const footerAction = canReadInKomga
    ? `<a class="btn btn-primary" href="${komgaBase()}/book/${esc(i.komga_book_id)}/read" target="_blank" rel="noopener">Open in Komga</a>`
    : `<button class="btn btn-primary" onclick="closeModal();openArcIssue(${s.id}, ${readingOrder})">Open in ${esc(i.source_title || 'its run')} →</button>`;
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(i.image_url, num)}
      <div class="issue-modal-info">
        <div class="issue-modal-num">${esc(i.source_title || '')} ${num}</div>
        <div class="issue-modal-series">${esc(s.title)} · reading order ${i.reading_order}</div>
        ${i.story_title ? `<div class="issue-modal-meta">${esc(i.story_title)}</div>` : ''}
        <div style="margin:10px 0">${statusChip}</div>
        <div class="issue-modal-details" style="font-size:12px;color:var(--tm);line-height:1.5">
          ${detailText}
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${footerAction}
    </div>
  `);
}

// Go to an arc issue in its OWN run (lazily created if needed, scoped to this arc's
// issues). Tracking-only — no download. Reached from the issue modal's action.
async function openArcIssue(arcId, readingOrder) {
  try {
    const r = await api.post(`/api/series/${arcId}/open-issue`, { reading_order: readingOrder });
    if (r.created) showToast('Created its run — tracking only, nothing downloaded');
    _pendingIssueOpen = { seriesId: r.series_id, number: r.number };
    navigate('series-detail', { id: r.series_id });
  } catch (e) {
    showToast('Couldn’t open issue — ' + (e?.message || e), 'error');
  }
}

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
          ? bookThumb(issue)
          : `/api/series/${s.id}/issues/${issue.number}/thumbnail`;
      inner = `<div class="issue-tile-img">
        <img src="${esc(thumbSrc)}" alt="${num}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">
      </div>`;
    } else {
      // variant_cover = your saved variant pick (not-yet-downloaded issues) — show it
      // here too so the grid tile matches the modal, falling back to the solicit cover.
      // No client-side art at all? Ask the server — its thumbnail route walks the
      // whole fallback chain (LOCG main → variant art). An issue with
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
      ? `<button class="issue-tile-search" title="Search for this issue" aria-label="Search for issue ${fmtNum(issue.number)}" data-dl="${s.id}:${issue.number}"
           onclick="event.stopPropagation(); searchIssue(${s.id}, ${issue.number}, this)">↓</button>`
      : '';
    return `<div class="issue-tile" data-num="${issue.number}" title="${esc(s.title)} ${num}" tabindex="0" role="button"
      onclick="showIssueModal(${s.id}, ${issue.number})"
      onkeydown="if(event.key==='Enter'||event.key===' ')showIssueModal(${s.id}, ${issue.number})">
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
let _detailPollId = null;        // the ONE live auto-populate poller (see renderSeriesDetail)

// Series whose auto-populate poll already ran to a conclusion this session.
// Without this latch the poller eats its own tail: its terminal branch calls
// renderSeriesDetail, that re-render hits the SAME arming gate (still 0 issues,
// still sitting in _autoSynced — which never clears), and arms a fresh 90s poll.
// Which times out, re-renders, arms again. Forever. The time-box that was meant
// to be the emergency brake quietly becomes the loop's period, and the thing
// hammers /api/series/{id} every 3s until you navigate away. A poll that reached
// ANY of its three exits has said all it's going to say — latch it shut. Only an
// explicit refresh earns a new one (see the pull-to-refresh handler).
const _autoPollDone = new Set();

// Arc ownership is a resolved SNAPSHOT (matched against Komga's book list), not a
// live join against issue_status — so "Get this storyline" queues downloads but the
// arc page's X/N counter sits frozen until something re-resolves it. Auto-fire once
// per arc per session on page view (mirrors _autoSynced) so revisiting the page after
// a grab shows current ownership without the user having to find the manual button.
const _autoArcResolved = new Set();
async function _autoResolveArc(id) {
  if (_autoArcResolved.has(id)) return;
  _autoArcResolved.add(id);
  try {
    await api.post(`/api/series/${id}/resolve-arc`, {});
    if (currentView === 'series-detail' && currentParams.id === id) renderSeriesDetail(id);
  } catch (e) { /* silent — the manual Refresh Ownership button still works */ }
}

// A source title minus the run it shares with the storyline's home, upper-cased for
// the cross-title tag: ('Detective Comics', 'Batman') -> 'DETECTIVE';
// ('Batman: Shadow of the Bat', 'Batman') -> 'SHADOW OF THE BAT'.
function _arcShortTitle(t, primary) {
  let s = (t || '').trim();
  if (primary && s.toLowerCase().startsWith(primary.toLowerCase())) {
    s = s.slice(primary.length).replace(/^[:\-\s]+/, '') || s;
  }
  return s.toUpperCase().replace(/\bCOMICS\b/, '').replace(/\s+/g, ' ').trim();
}

// Story arc detail — its issues span titles (Batman, Detective, …) and live in
// arc_issues, not issue_status. Per the spec it renders like the rest of the app:
// the SAME issue-tile grid as a series, with cross-title issues tagged in lime and
// the actions living in #topbar-actions (the app's consistent action bar).
function renderArcDetail(s) {
  const seriesBg = document.getElementById('series-bg');
  if (seriesBg) seriesBg.classList.add('hidden');
  const ai = s.arc_issues || [];
  const total = ai.length;
  // Back to the ORIGIN series' Arcs tab specifically — not just 'somewhere in
  // Detective Comics'. Reuses tracked_series_id already resolved on the first
  // reading-order row (same one showIssueModal's tiles use) instead of a
  // separate backend lookup. Lives in the tab-row spot, styled like a real tab,
  // because that's the one thing that spot should ever do — not a topbar
  // ghost-button, not a fake single-item tab pretending there's a choice.
  const originId = ai[0]?.tracked_series_id || null;
  const originTitle = ai[0]?.source_title || s.title;
  // The storyline's HOME run = the most-represented title; everything else is a
  // cross-title appearance and gets the lime tag.
  const counts = {};
  ai.forEach(i => { if (i.source_title) counts[i.source_title] = (counts[i.source_title] || 0) + 1; });
  const primary = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || '';
  const meta = ['◆ STORYLINE', s.publisher ? s.publisher.toUpperCase() : '', s.year_began,
    primary ? `originates in ${primary}` : ''].filter(Boolean).join('  •  ');

  // "Have it" = own the single OR covered by a collected edition (slice 5 makes
  // covered per-issue; for now s.collected is a whole-arc signal).
  const have = ai.filter(i => i.owned || s.collected).length;
  const collChip = s.collected ? `<span class="chip chip-collected" title="${esc(s.collection?.name || '')}">◆ collected</span>` : '';
  const chips = (total
    ? `<span class="chip ${have >= total ? 'chip-complete' : 'chip-missing'}">${have} / ${total} have</span>`
    : '') + collChip;

  const tiles = ai.map(i => {
    const owned = !!i.owned;
    const covered = !owned && s.collected;
    const stateCls = owned ? '' : covered ? ' covered' : ' missing';
    const num = `#${esc(String(i.number ?? '?'))}`;
    const cross = i.source_title && i.source_title !== primary;
    const xt = cross ? `<span class="issue-tile-xt u-truncate">${esc(_arcShortTitle(i.source_title, primary))}</span>` : '';
    const covBadge = covered ? '<div class="issue-tile-cov">◆</div>' : '';
    const cover = i.image_url
      ? `<img src="${esc(i.image_url)}" alt="${num}" loading="lazy" onerror="this.remove()">`
      : '';
    const ttl = `${esc(i.source_title || '')} ${num}${i.story_title ? ' — ' + esc(i.story_title) : ''}`;
    // The REAL issue modal (showIssueModal) — same one every series page uses, same
    // Download button, variants, LOCG details — scoped to the row's OWN home series
    // (tracked_series_id, resolved server-side). Falls back to the thin arc-only
    // modal only in the rare case a participating run somehow isn't resolved yet.
    const openIssue = i.tracked_series_id
      ? `showIssueModal(${i.tracked_series_id}, ${Number(i.number)})`
      : `showArcIssueModal(${i.reading_order})`;
    return `<div class="issue-tile" title="${ttl}" tabindex="0" role="button"
      onclick="${openIssue}"
      onkeydown="if(event.key==='Enter'||event.key===' ')${openIssue}">
      <div class="issue-tile-img${stateCls}">${cover}${covBadge}</div>
      <div class="issue-tile-num">${xt}${num}</div>
    </div>`;
  }).join('');

  const collBanner = (s.collected && s.collection) ? `
    <div class="arc-collected-note" tabindex="0" role="button" onclick="navigate('series-detail',{id:${s.collection.series_id}})"
      onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.collection.series_id}})">
      ◆ Owned as a collected edition — <strong>${esc(s.collection.name)}</strong>. The readlist builds from its volumes.
    </div>` : '';
  const body = total
    ? `<div class="issue-grid">${tiles}</div>`
    : `<div class="state-msg" style="padding:28px 0;font-size:12px;color:var(--tq)">Pulling reading order from ComicVine…</div>`;
  document.getElementById('topbar-title').textContent = s.title;
  document.getElementById('topbar-chips').innerHTML = chips;
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-primary btn-sm" id="arc-fulfill-btn" onclick="confirmFulfillArc(${s.id})">Get this storyline</button>
    <button class="btn btn-ghost btn-sm" onclick="buildArcReadlist(${s.id}, this)">Build Komga readlist</button>
    <button class="btn btn-ghost btn-sm" onclick="refreshArcOwnership(${s.id}, this)">Refresh ownership</button>
  `;
  document.getElementById('topbar-sub').innerHTML =
    `<span class="series-meta-text u-truncate">◆ storyline · issues live in their own runs${primary ? ` · originates in ${esc(primary)}` : ''}</span>`;

  setApp(`
    <div class="issue-tabs-row">${originId ? `<div class="issue-tab active" tabindex="0" role="button"
        onclick="detailTab='arcs';navigate('series-detail',{id:${originId}})"
        onkeydown="if(event.key==='Enter'||event.key===' '){detailTab='arcs';navigate('series-detail',{id:${originId}})}">
        ← Back to ${esc(originTitle)} Arcs</div>` : ''}</div>
    ${collBanner}
    ${body}
  `);
  // Just-added arc whose background populate hasn't landed yet — re-fetch once.
  if (!total) setTimeout(() => {
    if (currentView === 'series-detail' && currentParams.id === s.id) renderSeriesDetail(s.id);
  }, 4000);
  if (total) _autoResolveArc(s.id);
}

// GET is a deliberate, destructive action: it materialises every participating run
// (creating series + folders for Detective, Showcase, …) and queues downloads. Warn
// before doing any of that — no silent folder creation. Themed modal, not a native
// confirm() — the browser dialog was the one stock-white popup in an otherwise
// fully-skinned app.
function confirmFulfillArc(id) {
  const title = _detailSeries?.title || 'this storyline';
  showModal(`
    <div class="modal-title">Get this storyline?</div>
    <div class="confirm-body">
      Get <strong style="color:var(--tp)">${esc(title)}</strong>?
      <div class="confirm-note">Creates a tracked series + folder for every participating run
        and queues downloads of the missing issues into them. Nothing is created until you confirm.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="doFulfillArc(${id})">Get this storyline</button>
    </div>
  `);
}

async function doFulfillArc(id) {
  closeModal();
  const btn = document.getElementById('arc-fulfill-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Queueing…'; }
  try {
    const r = await api.post(`/api/series/${id}/fulfill`, {});
    if (r.queued === 0) {
      showToast(r.owned >= r.total ? 'Arc already complete — nothing to pull' : 'Nothing missing to queue');
    } else {
      showToast(`Fulfilling arc — queued ${r.queued} missing issue${r.queued === 1 ? '' : 's'} into their runs`);
    }
  } catch (e) {
    showToast('Fulfill failed — ' + (e?.message || e), 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Get this storyline'; }
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
    if (btn) { btn.disabled = false; btn.textContent = 'Build Komga readlist'; }
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
  // No topbar back-button — LIBRARY already lives permanently in the sidebar nav,
  // so a second '← Library' here was pure duplication, not a distinct nav need.
  // Still clear the topbar (drops stale actions from whatever view was active
  // before — Activity's Sweep/Start Queue buttons, etc.).
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');

  const s = await api.get(`/api/series/${id}`);
  _detailSeries = s;
  // Ask for Related NOW, before the grid's hundred cover requests queue up in
  // front of it — the row paints once the grid is there.
  const relP = api.get(`/api/series/${id}/related`).catch(() => null);
  if (s.kind === 'arc') return renderArcDetail(s);

  // Self-healing: a never-synced or stale (>1h) series refreshes itself on view,
  // in the background — no manual Sync button needed. Fires at most once per
  // series per session so a persistently-failing sync can't loop. A successful
  // sync updates last_synced (no longer stale) and re-renders.
  const _lastMs = s.last_synced ? Date.parse(s.last_synced.replace(' ', 'T') + 'Z') : 0;
  // Pulled series: stale after an hour. Not pulled: after a week — the weekly
  // check is the cadence for those, and opening one shouldn't hit LOCG daily.
  const _staleMs = s.on_pull_list ? 3600000 : 7 * 86400000;
  if ((Date.now() - _lastMs) > _staleMs && !_autoSynced.has(id)) {
    _autoSynced.add(id);
    syncSeries(id, null, s);   // s was fetched 10 lines up — don't GET it again
  }

  const meta = [s.publisher ? s.publisher.toUpperCase() : '', s.year_began].filter(Boolean).join('  •  ');
  const total = s.owned + s.missing + s.upcoming;
  const released = s.owned + s.missing;

  // B1: a trade-only series (no singles, but collected editions exist) lands you on
  // Trades — otherwise you hit an empty Issues grid while the real content hides one
  // tab over. One-shot per entry (_autoTabFor) so a manual tab pick afterward sticks.
  if (_autoTabFor !== id) {
    // Series→series hop (arc issue click, Activity row): navigate() only resets
    // detailTab when LEAVING detail, so series B used to inherit series A's tab.
    if (_autoTabFor !== null) detailTab = 'all';
    if (total === 0 && s.has_trades) detailTab = 'trades';
    _autoTabFor = id;
  }

  const chips = [
    released > 0 ? `<span class="chip ${s.owned < released ? 'chip-missing' : 'chip-complete'}">${s.owned}/${released}</span>` : '',
    s.upcoming ? `<span class="chip chip-upcoming">${s.upcoming} upcoming</span>` : '',
    s.from_list_id ? `<a class="chip chip-collected" title="Tracked by a reading list's Get — pull list off, only what the list asked for" onclick="navigate('readlist', {id: ${s.from_list_id}})">◆ from a reading list</a>` : '',
  ].filter(Boolean).join('');

  // An on/off switch, not a button: pulling is a state you can see and undo.
  // On = actively download new + missing issues. Off = know about it, check weekly.
  const pullBtn = `<label class="pull-switch" title="${s.on_pull_list
      ? 'On the pull list: new and missing issues are downloaded'
      : 'Not pulled: Kometa checks for new issues weekly but downloads nothing'}">
    <span class="u-label">Pull list</span>
    <span class="toggle-switch">
      <input type="checkbox" role="switch" aria-label="Pull list" ${s.on_pull_list ? 'checked' : ''}
        onchange="togglePullList(${s.id}, this.checked)">
      <span class="toggle-track"><span class="toggle-thumb"></span></span>
    </span>
  </label>`;

  // Oversized = lift the single-issue page-count guard to 150 for this series.
  // For the quarterly bricks (Head Lopper et al.) whose legit issues bust the
  // default 70-page webtoon/collection ceiling and get rejected on repeat.
  const oversizedBtn = `<button class="btn btn-sm ${s.page_max ? 'btn-primary' : 'btn-ghost'}"
    title="${s.page_max ? `Page limit raised to ${s.page_max}` : 'Raise the single-issue page limit for oversized formats'}"
    onclick="toggleOversized(${s.id}, ${!s.page_max})">Oversized</button>`;

  // Arcs are a ComicVine feature — no CV, no arc tab. Coerce a stale 'arcs'
  // selection (toggle flipped off while it was the active tab) back to 'all'.
  const _arcsOn = !!_appConfig.comicvine_enabled;
  if (detailTab === 'arcs' && !_arcsOn) detailTab = 'all';
  const _tabList = _arcsOn
    ? ['all','owned','missing','upcoming','trades','arcs']
    : ['all','owned','missing','upcoming','trades'];
  const tabs = _tabList.map(t => {
    // Trades tab carries a count badge when collected editions exist (from cache).
    const badge = (t === 'trades' && s.trade_count)
      ? `<span class="tab-badge">${s.trade_count}</span>`
      : (t === 'arcs' && s.arc_count) ? `<span class="tab-badge">${s.arc_count}</span>` : '';
    return `<div class="issue-tab ${detailTab === t ? 'active' : ''}" tabindex="0" role="tab" aria-selected="${detailTab === t}"
      onclick="setDetailTab('${t}', ${id})"
      onkeydown="if(event.key==='Enter'||event.key===' ')setDetailTab('${t}', ${id})">${t}${badge}</div>`;
  }).join('');

  const tiles = (detailTab === 'trades' || detailTab === 'arcs') ? '' : buildIssueTiles(s);

  const seriesBg = document.getElementById('series-bg');
  const seriesBgImg = document.getElementById('series-bg-img');
  // Random issue cover as the backdrop — different each visit, not always the same one.
  // Build candidates off the SAME cover chain the grid tiles use (komga → LOCG art →
  // per-issue thumbnail route), not the raw metron_image column. LOCG-only series (e.g.
  // one-shots) never stamp metron_image, so keying off it alone left them with a dead
  // /series/{id}/thumbnail fallback and a blank backdrop. The per-issue route self-heals.
  const _covers = (s.issues || []).map(i =>
    i.komga_book_id ? bookThumb(i)
      : _metronArt(i) || `/api/series/${s.id}/issues/${i.number}/thumbnail`);
  const _bg = _covers.length
    ? _covers[Math.floor(Math.random() * _covers.length)]
    : `/api/series/${s.id}/thumbnail`;
  seriesBgImg.style.backgroundImage = `url("${_bg}")`;
  seriesBg.classList.remove('hidden');

  document.getElementById('topbar-title').textContent = s.title;
  document.getElementById('topbar-chips').innerHTML = chips;
  document.getElementById('topbar-actions').innerHTML = `
    ${_favBtn(s)}
    ${pullBtn}
    ${oversizedBtn}
    ${s.missing > 0 ? `<button class="btn btn-ghost btn-sm" onclick="sweepSeries(${s.id}, this)">Sweep Missing</button>` : ''}
    ${s.folder_path && s.match_status !== 'pending' && s.match_status !== 'needs_match'
      ? `<button class="btn btn-ghost btn-sm" title="Rename files and folder to the house convention (dry run first)" onclick="_tidyPlan(${s.id}, this)">Tidy</button>` : ''}
    <button class="btn btn-ghost btn-sm" onclick="confirmDelete(${s.id})">Remove</button>
  `;
  document.getElementById('topbar-sub').innerHTML = `
    <span class="series-meta-text">${esc(meta)}</span>
    <button class="btn btn-ghost btn-sm" title="Folder path" aria-label="Folder path" onclick="showFolderPathModal(${s.id})">${_FF_SVG}</button>
  `;

  const matchBanner = (s.match_status === 'needs_match' || s.match_status === 'pending')
    ? `<div class="match-banner" id="match-banner">
        <div class="match-head">
          <div class="match-banner-text">${s.match_status === 'pending'
            ? '<b>Not matched yet.</b>'
            : '<b>Pick the run.</b> More than one series could be this folder, or none clearly fits.'}</div>
          <div class="match-head-actions">
            ${s.match_status === 'pending' ? `<button class="btn btn-primary btn-sm" onclick="_matchNow(${s.id}, this)">Match now</button>` : ''}
            ${s.match_status === 'needs_match' ? `<button class="btn btn-ghost btn-sm" title="There is no run to pick: an omnibus library, a folder of specials, a fan book. Keep it as a shelf series." onclick="_noRun(${s.id})">No run</button>` : ''}
            <button class="btn btn-ghost btn-sm match-remove" title="Remove this series and move its folder to the bin"
              onclick="confirmDelete(${s.id})">Remove</button>
          </div>
        </div>
        ${`
        <div class="match-search"><input class="browse-search" id="match-q" value="${esc(s.title)}" placeholder="Search by title"
          onkeydown="if(event.key==='Enter')_loadMatchCandidates(${s.id}, this.value)">
          <button class="btn btn-ghost btn-sm" onclick="_loadMatchCandidates(${s.id}, document.getElementById('match-q').value)">Search</button></div>
        <div class="match-results" id="match-results"><div class="match-hint">Searching…</div></div>`}
      </div>` : '';

  setApp(`
    ${matchBanner}
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
          // Only claim "Syncing…" while a sync is genuinely pending (never synced). Once
          // last_synced is set and there are still no issues, the sync is DONE and empty —
          // say so, don't spin a lie the user has to reload to escape.
          : s.locg_series_id && total === 0 ? (s.last_synced ? 'No issues found for this series.' : 'Syncing issues…')
          : 'Nothing here.'}</div>`}</div>`}
  `);

  if (detailTab === 'trades') _loadTradesPanel(id);
  if (detailTab === 'arcs') _loadArcsPanel(id);
  if (s.match_status === 'needs_match' || s.match_status === 'pending') _loadMatchCandidates(id, s.title);
  if (s.match_status === 'pending' || s.match_status === 'needs_match') _showLocgPause(id);
  if (s.shelf_id && (detailTab === 'all' || detailTab === 'owned')) _loadShelfFiles(s, total === 0);
  if (detailTab === 'all') relP.then(d => d && _paintRelated(id, d));

  // Arrived by clicking an arc issue (openArcIssue) → open that issue's modal now
  // that its run is loaded. Cleared so it fires once.
  if (_pendingIssueOpen && _pendingIssueOpen.seriesId === id) {
    const num = _pendingIssueOpen.number;
    _pendingIssueOpen = null;
    if (num != null && s.issues?.some(i => i.number === num)) {
      setTimeout(() => showIssueModal(id, num), 50);
    }
  }

  // Auto-populate: while a sync is in flight, poll and re-render so issues appear on
  // their own — no manual refresh. Runs for a fresh add (never synced) or an active
  // re-sync this session; a trade-only series is skipped (its singles never come — that
  // was the old forever-spinner). Re-render fires when issues land OR when the sync
  // completes at all (last_synced advances) — so "Syncing…" resolves to "No issues
  // found" instead of spinning. Time-boxed so a crashed sync (no mark_synced) stops.
  // The id lives at module scope so re-entering this render (tab clicks route
  // through setDetailTab → here) REPLACES the poller instead of stacking a new
  // 3s loop on top of the old one every visit.
  clearInterval(_detailPollId);
  if (s.locg_series_id && total === 0 && !s.has_trades && detailTab !== 'trades'
      && (!s.last_synced || _autoSynced.has(id)) && !_autoPollDone.has(id)) {
    const _syncedAt = s.last_synced || null;
    const _stopAt = Date.now() + 90000;
    const _pollId = _detailPollId = setInterval(async () => {
      if (currentView !== 'series-detail' || currentParams.id !== id) { clearInterval(_pollId); return; }
      const fresh = await api.get(`/api/series/${id}`).catch(() => null);
      if (!fresh) { clearInterval(_pollId); return; }
      const freshTotal = fresh.owned + fresh.missing + fresh.upcoming;
      if (freshTotal > 0 || (fresh.last_synced && fresh.last_synced !== _syncedAt) || Date.now() > _stopAt) {
        clearInterval(_pollId);
        _autoPollDone.add(id);   // spent — the re-render on the next line must NOT re-arm it
        renderSeriesDetail(id);
      }
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
  body.innerHTML = `<div class="issue-grid">${arcs.map((a, i) => _arcRowHtml(a, i)).join('')}</div>`;
}

let _arcsPanelData = [];
let _arcsPanelSeriesId = null;

// Arcs render as the SAME issue-tile its sibling tabs (Issues, Trades) use — one
// consistent tile size within a series' detail, not the bigger library card. The
// ◆/◇ ARC badge rides the corner slot trades use for the format; the name + count
// is the bottom label. Tracked → navigate; discovered → populate-then-navigate.
function _arcRowHtml(a, i) {
  const tracked = !!a.tracked;
  const total = a.issue_count || 0;
  const owned = a.owned_count || 0;
  const complete = tracked && total > 0 && owned >= total;
  const cover = a.image
    ? `<img src="${esc(a.image)}" alt="${esc(a.name)}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">`
    : '';
  // Incomplete/undiscovered arcs get the amber 'missing' outline, like a missing trade.
  const stateCls = complete ? '' : ' missing';
  const badge = `<div class="series-card-next-release">${tracked ? '◆' : '◇'} ARC</div>`;
  const label = tracked && total ? `${esc(a.name)} · ${owned}/${total}` : esc(a.name);
  const click = tracked
    ? `onclick="navigate('series-detail',{id:${a.id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${a.id}})"`
    : `onclick="openDiscoveredArc(${i}, this)" onkeydown="if(event.key==='Enter'||event.key===' ')openDiscoveredArc(${i},this)"`;
  return `<div class="issue-tile arc-tile${tracked ? '' : ' arc-discovered'}" title="${esc(a.name)}" tabindex="0" role="button" ${click}>
    <div class="issue-tile-img${a.image ? stateCls : ' unknown'}">
      ${cover}
      ${badge}
    </div>
    <div class="issue-tile-num">${label}</div>
  </div>`;
}

async function openDiscoveredArc(i, row) {
  const a = _arcsPanelData[i];
  if (!a) return;
  const cnt = row && row.querySelector('.issue-tile-num');
  if (cnt) cnt.textContent = 'opening…';
  if (row) row.style.opacity = '0.5';
  try {
    const arc = await api.post(`/api/series/${_arcsPanelSeriesId}/arcs/populate`,
      { name: a.name, cv_arc_id: a.cv_arc_id, first_issue: a.first_issue, last_issue: a.last_issue });
    navigate('series-detail', { id: arc.id });
  } catch (e) {
    showToast('Couldn’t open arc — ' + (e?.message || e), 'error');
    if (row) row.style.opacity = '';
    if (cnt && a) cnt.textContent = a.name;
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
      data.reason === 'locg_paused' ? 'Trades come from LOCG, and LOCG is pausing us right now — try again later.'
      : data.reason === 'no_locg_match' ? 'Trades come from LOCG, and it doesn\'t list this run clearly. Pick it on LOCG from the match banner to see its trades.'
      : data.reason === 'no_locg_id' ? 'No LOCG link for this series — can\'t look up trades.' : 'No collected editions found.'}</div>`;
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
  const tag = _volLabel(t);
  // Same tile skeleton as issues so the grid stays visually identical. The format
  // (TPB/HC) rides in the corner badge slot; the volume is the bottom label.
  const cover = t.cover ? `<img src="${esc(t.cover)}" alt="${esc(tag)}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">` : '';
  // Owned and on the shelf → the tile reads it, like an owned issue. Otherwise
  // the details modal (with its download) as before.
  const go = (t.owned && t.book_id) ? `navigate('read', {book: ${t.book_id}})`
    : t.locg_id ? `showTradeModal('${esc(t.locg_id)}')` : '';
  const click = go ? ` tabindex="0" role="button" onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}"` : '';
  // Owned (file on disk) → no download arrow, owned styling. Otherwise the same
  // ↓ arrow missing singles get; stopPropagation so the tile click doesn't fire.
  const dlBtn = (t.locg_id && !t.owned)
    ? `<button class="issue-tile-search" title="Download this trade" aria-label="Download trade ${esc(tag)}" data-dl="trade:${t.locg_id}"
         onclick="event.stopPropagation(); tradeDownload('${esc(t.locg_id)}', this)">↓</button>`
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
  const tag = _volLabel(t);
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(t.cover, tag)}
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
  const label = _volLabel(t);
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

function showFolderPathModal(seriesId) {
  const s = _detailSeries;
  showModal(`
    <div class="modal-title">Folder Path</div>
    <div class="confirm-body">
      Where this series lives on disk. Kometa uses this to detect what you already
      own — changing it re-scans the new location.
    </div>
    <div style="margin-top:12px">
      ${folderField('series' + seriesId, s?.folder_path, 'library', async (path) => {
        await api.patch(`/api/series/${seriesId}/folder`, { folder_path: path || null });
        renderSeriesDetail(seriesId);
      })}
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
    </div>
  `);
}

async function togglePullList(id, on) {
  let res;
  try {
    res = await api.patch(`/api/series/${id}/pull-list`, { on_pull_list: on });
  } catch (e) {
    showToast('Pull-list update failed'); console.error(e); renderSeriesDetail(id); return;
  }
  showToast(on ? 'On the pull list — searching for missing issues now'
    : `Off the pull list${res?.cancelled ? ` — ${res.cancelled} queued search${res.cancelled === 1 ? '' : 'es'} cancelled` : ''}`);
  renderSeriesDetail(id);
}

async function toggleOversized(id, on) {
  try {
    await api.patch(`/api/series/${id}/page-max`, { page_max: on ? 150 : null });
  } catch (e) {
    showToast('Oversized update failed'); console.error(e); return;
  }
  showToast(on ? 'Oversized format: page limit raised to 150' : 'Oversized format off');
  renderSeriesDetail(id);
}


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
let _wizardState = { idx: -1, source: 'locg', locgId: null };

function showAddWizard(query = '') {
  // Can't track or file a series with nowhere to put it. If the comics folder
  // isn't usable, channel the user to set it first — just-in-time, not a gate.
  if (!_appConfig.comics_root_ok) { _showComicsRootSetup(); return; }
  if (query) setTimeout(() => { const i = document.getElementById('wizard-search'); if (i) { i.value = query; wizardSearch(); } }, 50);
  _wizardResults = [];
  _wizardLastQuery = '';
  _wizardState = { idx: -1, source: 'locg', locgId: null };
  showModal(`
    <div class="modal-title">Add Series</div>
    <div class="wizard-search-row">
      <input class="search-input" id="wizard-search" type="search" spellcheck="false" placeholder="Search for a series…" autocomplete="off"
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

function _showComicsRootSetup(prefill) {
  const cur = prefill != null ? prefill : (_appConfig.comics_root || '');
  showModal(`
    <div class="modal-title">Set your comics folder</div>
    <div style="font-size:12px;color:var(--tq);margin:8px 0 14px;line-height:1.5">
      Kometa needs somewhere to file comics before it can track or download them.
      It's the one thing it needs — everything else is optional.
    </div>
    <div class="settings-field">
      <div class="settings-field-label u-label">Comics library path</div>
      ${folderField('setup', cur, 'fs', () => {}, { whisper: false, reopen: (picked) => _showComicsRootSetup(picked) })}
    </div>
    <div id="setup-root-err" style="font-size:11px;color:var(--amb);margin-top:6px;min-height:14px"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="setup-root-btn" onclick="saveComicsRoot(this)">Save &amp; Continue</button>
    </div>
  `);
  setTimeout(() => document.getElementById('ff-setup')?.focus(), 50);
}

// PATCH the comics root and refresh health. Returns true if it's now usable.
async function _commitComicsRoot(path) {
  await api.patch('/api/config', { comics_root: path });
  _appConfig = await api.get('/api/config');
  return _appConfig.comics_root_ok;
}

async function saveComicsRoot(btn) {
  const path = document.getElementById('ff-setup').value.trim();
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
    // WHAT YOU TYPED WINS. Title match is the first gate, BEFORE kind — otherwise a
    // long run like "100 Bullets" buries its own series under 20+ inner arcs
    // (Tarantula, Punch Line, A Wake…) and the slice(0,15) below guillotines the
    // series clean off the list. Type a series name → see the series. Type an event
    // name ("Knightfall") → its storyline is the exact match and still leads. Same
    // gate serves both.
    const at = (a.series || a.name || '').toLowerCase();
    const bt = (b.series || b.name || '').toLowerCase();
    const aExact = at === ql, bExact = bt === ql;
    if (aExact !== bExact) return aExact ? -1 : 1;
    const aPrefix = at.startsWith(ql), bPrefix = bt.startsWith(ql);
    if (aPrefix !== bPrefix) return aPrefix ? -1 : 1;
    // Same title-match tier? Rank by kind: storyline (the run an event originates in)
    // first, then a real SERIES, then an arc, then a collected edition. SERIES beats
    // ARC on purpose — search "100 Bullets" and a distinct spin-off (Brother Lono,
    // The US of Anger) is something you'd actually ADD, while an inner arc (Tarantula,
    // Punch Line…) you'll find on the run's own Arcs tab. Letting those inner arcs
    // outrank the spin-offs chopped them off the slice(0,15) cliff. Collected editions
    // still sink to the bottom — that was always the real intent, "arc beats a single
    // collected volume," never "arc beats a series."
    const rank = r => r.kind === 'storyline' ? 0 : _isCollectedResult(r) ? 3 : r.kind === 'arc' ? 2 : 1;
    return rank(a) - rank(b);
  });
  _wizardResults = results.slice(0, 15);
  _wizardHi = -1;   // fresh results, fresh keyboard cursor

  const paint = () => {
    const el = document.getElementById('wizard-results');
    if (!el) return;
    el.innerHTML = _wizardResults.length
      ? _wizardResults.map((r, i) => `
          <div class="wizard-result wizard-result-enter" style="animation-delay:${i * STAGGER_MS}ms" onclick="wizardPickSeries(${i})">
            <img class="wizard-result-thumb" src="${r.kind === 'storyline' ? '' : esc(r.cover || r.image || '')}" alt=""
              onerror="this.style.opacity=0" loading="lazy">
            <div class="wizard-result-text">
              <div class="wizard-result-title u-truncate">${esc(r.series || r.name || '')}${r.kind === 'storyline' ? ' <span class="locg-badge">◆ STORYLINE</span>' : r.kind === 'arc' ? ' <span class="locg-badge">◆ ARC</span>' : _isCollectedResult(r) ? ' <span class="locg-badge collected-badge">◆ COLLECTED</span>' : r.needs_resolve ? ' <span class="locg-badge">◆ ONE-SHOT</span>' : r.source === 'locg' ? ' <span class="locg-badge">LOCG</span>' : ''}</div>
              <div class="wizard-result-meta">${r.kind === 'storyline' ? `story arc · originates in ${esc(r.origin_title || '?')}${r.origin_year ? ' (' + r.origin_year + ')' : ''}` : `${esc(r.publisher?.name || '')}${r.kind === 'arc' ? ' · story arc' : r.needs_resolve ? ' · one-shot → its series' : _isCollectedResult(r) ? ' · collected edition — lives in a series’ Trades' : ''}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}`}</div>
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
    // Storylines (arc-first): each resolved to the run it originates in. Runs in
    // parallel and merges to the TOP of every result set — searching "Knightfall"
    // leads with "originates in Batman (1940)", whatever else the title search finds.
    const slP = api.get(`/api/search/storyline?q=${encodeURIComponent(q)}`).catch(() => []);

    const locgResults = await api.get(`/api/search/locg?q=${encodeURIComponent(q)}`);
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    if (locgResults.length) {
      const arcs = (await cvP).filter(r => r.kind === 'arc');
      _renderWizardResults([...(await slP), ...arcs, ...locgResults], q); return;
    }

    // LOCG empty — show the full ComicVine result (arcs + collected-edition volumes).
    const cvResults = await cvP;
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    _renderWizardResults([...(await slP), ...cvResults], q);
  } catch (err) {
    console.error('wizardSearch error:', err);
    _wizardLastQuery = '';   // let a retry through — this query never landed
    if (seq !== _wizardSeq) return;
    const el = document.getElementById('wizard-results');
    if (el) el.innerHTML = `<div class="state-msg" style="padding:16px 0;font-size:11px;color:var(--amb)">Search failed: ${esc(String(err))}</div>`;
  }
}

// A one-shot LOCG returned at the issue level: its id is a COMIC id, not a series id.
// Resolve it up to its parent series (one fetch, on pick — not per keystroke), swap the
// result in place, then fall back into the normal series-pick flow. Strip the trailing
// "#N" so the swapped title reads like the series, not the single issue.
async function _wizardResolveComic(idx) {
  const r = _wizardResults[idx];
  const m = document.getElementById('modal');
  if (m) m.innerHTML = `<div class="modal-title">Finding series…</div>
    <div class="state-msg" style="padding:16px 0;font-size:11px;color:var(--tq)">Resolving “${esc(r.series || '')}” to its series on LOCG…</div>`;
  try {
    const res = await api.get(`/api/search/locg/resolve?comic_id=${encodeURIComponent(r.id)}&slug=${encodeURIComponent(r.slug || '')}`);
    // Keep the origin comic id + slug on the result so confirm can forward them — the
    // SERVER re-resolves authoritatively at add time, so a comic id can't slip through.
    _wizardResults[idx] = { ...r, id: res.series_id, series: (r.series || '').replace(/\s*#\s*\d+.*$/, '').trim(), needs_resolve: false, _comicId: r.id, _comicSlug: r.slug };
    wizardPickSeries(idx);
  } catch (e) {
    if (m) m.innerHTML = `<div class="modal-title">Couldn’t resolve</div>
      <div class="state-msg" style="padding:16px 0;font-size:11px;color:var(--amb)">“${esc(r.series || '')}” isn’t linked to a series on LOCG, so it can’t be tracked as one.</div>
      <div class="modal-footer"><button class="btn btn-ghost" onclick="showAddWizard()">← Back</button></div>`;
  }
}

function wizardPickSeries(idx) {
  const r = _wizardResults[idx];
  if (!r) return;
  if (r.needs_resolve) return _wizardResolveComic(idx);
  if (r.kind === 'storyline') return wizardPickStoryline(idx);
  const isArc = r.kind === 'arc';
  // Every source the search can hand back has to survive to confirm: LOCG ids,
  // ComicVine volumes (the fallback while LOCG is shut), Metron ids. 'Track
  // Series' used to return silently on anything but LOCG — a dead click.
  _wizardState = { idx, source: isArc ? 'arc' : (r.source || 'locg'), locgId: r.source === 'locg' ? r.id : null,
    cvArcId: isArc ? r.cv_arc_id : null, cvVolumeId: (!isArc && r.cv_volume_id) ? r.cv_volume_id : null,
    metronId: r.source === 'metron' ? r.id : null };
  // An arc owns no folder (lens model): it tracks a cross-title reading order and
  // grabs the collected edition into its main series. So no folder field, and the
  // pull-list line means 'find the collected edition', not 'download every issue'.
  const folderBlock = isArc ? `
    <div class="wizard-arc-note">◆ Story arc — Kometa tracks the reading order across every participating title. It owns no folder; the collected edition lands in its main series' Trades.</div>`
    : `
    <div class="step-label u-label" style="margin-top:16px;margin-bottom:6px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--tq)">Folder path <span style="color:var(--tq);font-weight:400;text-transform:none">(auto-detected — edit if needed)</span></div>
    ${folderField('wizard', '', 'library', () => {}, { whisper: false, reopen: (picked) => {
      wizardPickSeries(idx);   // browsing replaced the wizard modal — rebuild it, then drop the pick in
      setTimeout(() => { const i = document.getElementById('ff-wizard'); if (i) { i.value = picked; _ffDirty('wizard'); } }, 20);
    } })}
    <div id="wizard-folder-hint" style="margin-top:6px;font-size:10px;color:var(--tq)">&nbsp;</div>`;
  document.getElementById('modal').innerHTML = `
    <div class="modal-title">Add ${isArc ? 'Story Arc' : 'Series'}</div>
    <div class="wizard-series-preview">
      <img class="wizard-result-thumb" src="${esc(r.cover || r.image || '')}" alt="" onerror="this.style.opacity=0">
      <div class="wizard-result-text">
        <div class="wizard-result-title u-truncate">${esc(r.series || r.name || '')}${isArc ? ' <span class="locg-badge">◆ ARC</span>' : ''}</div>
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
    const input = document.getElementById('ff-wizard');
    const hint = document.getElementById('wizard-folder-hint');
    if (!input) return;
    input.placeholder = res.path;
    if (!input.value) { input.value = res.path; _ffDirty('wizard'); }   // pre-fill, still editable
    if (hint) {
      hint.textContent = res.exists ? '✓ Existing folder — owned issues will be detected' : 'New folder — will be created on first download';
      hint.style.color = res.exists ? 'var(--pri)' : 'var(--tq)';
    }
  } catch (e) {
    const input = document.getElementById('ff-wizard');
    if (input) input.placeholder = '/comics/Publisher/Series Name';
  }
}

// Following a storyline is the arc-first move: you don't add the arc, you add the
// RUN it originates in, as a normal tracked series — nothing downloads. The arc then
// lives under that series' Arcs tab. So this panel has no folder picker and no
// pull-list toggle; it's a deliberate "track this run" with the storyline as the why.
function wizardPickStoryline(idx) {
  const r = _wizardResults[idx];
  if (!r) return;
  _wizardState = {
    idx, source: 'storyline',
    cvVolumeId: r.origin_volume_id,
    originTitle: r.origin_title,
    originYear: r.origin_year,
    originPublisher: r.origin_publisher,
  };
  const run = `${esc(r.origin_title || '?')}${r.origin_year ? ' (' + r.origin_year + ')' : ''}`;
  document.getElementById('modal').innerHTML = `
    <div class="modal-title">Follow Storyline</div>
    <div class="wizard-series-preview">
      <div class="wizard-result-text">
        <div class="wizard-result-title u-truncate">${esc(r.series || '')} <span class="locg-badge">◆ STORYLINE</span></div>
        <div class="wizard-result-meta">${esc(r.origin_publisher || '')}${r.origin_publisher ? ' · ' : ''}originates in ${run}</div>
      </div>
    </div>
    <div class="wizard-arc-note">Following adds <b>${run}</b> as a tracked series — its Issues, Trades and Arcs tabs. <b>Nothing downloads;</b> this is tracking only. The storyline then lives under that series' Arcs tab.</div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="showAddWizard()">← Back</button>
      <button class="btn btn-primary" id="wizard-add-btn" onclick="wizardConfirm()">Follow → add ${esc(r.origin_title || 'series')}</button>
    </div>
  `;
}

async function wizardConfirm() {
  const { source, locgId, cvArcId, cvVolumeId } = _wizardState;
  if (source === 'storyline') {
    const r = _wizardResults[_wizardState.idx];
    const btn = document.getElementById('wizard-add-btn');
    btn.disabled = true; btn.textContent = 'Following…';
    try {
      // Add the ORIGIN RUN, anchored to its CV volume, tracking only (no pull list).
      const added = await api.post('/api/series', {
        cv_volume_id: cvVolumeId,
        title: _wizardState.originTitle,
        publisher_name: _wizardState.originPublisher || '',
        year_began: _wizardState.originYear || null,
        on_pull_list: false,
      });
      closeModal();
      showToast(`Following ${added.title} — tracking only`);
      navigate('series-detail', { id: added.id });
    } catch (e) {
      btn.disabled = false;
      btn.textContent = `Follow → add ${_wizardState.originTitle || 'series'}`;  // restore the real label, not a stub
      showToast('Follow failed — try again');
      console.error(e);
    }
    return;
  }
  const { cvVolumeId: cvVol, metronId } = _wizardState;
  if (!locgId && !cvArcId && !cvVol && !metronId) { showToast('This result can’t be tracked from here', 'error'); return; }
  const r = _wizardResults[_wizardState.idx];
  const folder = (document.getElementById('ff-wizard')?.value || '').trim() || null;
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
    } else if (source === 'comicvine' || (!locgId && cvVol)) {
      payload.cv_volume_id = cvVol;
      payload.title = r.series || r.name || '';
      payload.publisher_name = r.publisher?.name || '';
      payload.year_began = r.year_began || null;
    } else if (source === 'metron') {
      payload.metron_id = metronId;
      payload.title = r.series || r.name || '';
      payload.publisher_name = r.publisher?.name || '';
      payload.year_began = r.year_began || null;
    } else if (source === 'locg') {
      payload.locg_id = locgId;
      payload.title = r.series || r.name || '';
      payload.publisher_name = r.publisher?.name || '';
      payload.year_began = r.year_began || null;
      // One-shot resolved on pick: forward the origin comic id + slug so the server
      // re-resolves to the parent series authoritatively (never anchors a comic id).
      if (r._comicId) { payload.locg_comic_id = r._comicId; payload.locg_comic_slug = r._comicSlug; }
    }
    const added = await api.post('/api/series', payload);
    closeModal();
    navigate('series-detail', { id: added.id });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = _wizardState.source === 'arc' ? 'Track Arc' : 'Track Series';
    showToast('Add failed — try again');
    console.error(e);
  }
}

// --- Folder Field: ONE aligned pattern for every place you pick a folder ---
// Icon-in-box browses via the shared modal; the input autosaves on blur; an
// empty field glows the icon lime. Each field registers its save() by id; the
// modal, the browse flow, and the "saved" whisper are shared.
const _FF_SVG = '<svg aria-hidden="true" width="15" height="14" viewBox="0 0 15 14" fill="none"><path d="M1 3h4.5l1.2 1.8H14V12H1V3z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>';
const _ffReg = {};

function folderField(id, path, scope, save, opts = {}) {
  _ffReg[id] = { scope, save, whisper: opts.whisper !== false, reopen: opts.reopen };
  const p = path || '';
  return `<div class="folder-field${p ? '' : ' empty'}" data-ffid="${id}">
    <div class="ff-box">
      <button class="ff-ico" type="button" title="Browse for folder" aria-label="Browse for folder" onclick="_ffBrowse('${id}')">${_FF_SVG}</button>
      <input class="ff-input" id="ff-${id}" value="${esc(p)}" placeholder="Choose a folder…"
        autocomplete="off" spellcheck="false"
        oninput="_ffDirty('${id}')" onchange="_ffCommit('${id}')"
        onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur();}">
    </div>
    <span class="ff-whisper" id="ffw-${id}">saved</span>
  </div>`;
}

function _ffDirty(id) {   // typing un-empties the field (kills the lime glow)
  const f = document.querySelector(`.folder-field[data-ffid="${id}"]`);
  if (f) f.classList.toggle('empty', !(document.getElementById('ff-' + id)?.value || '').trim());
}

function _ffBrowse(id) {
  const reg = _ffReg[id];
  if (!reg) return;
  _fbScope = reg.scope;
  // Open one level UP from the current folder, so you land among its siblings
  // (the current one included) rather than inside a usually-empty leaf. Empty
  // field → the scope's default landing spot.
  const cur = (document.getElementById('ff-' + id)?.value || '').trim();
  const start = cur ? cur.replace(/\/[^/]*$/, '') : '';
  // reopen: for a field that lives INSIDE a modal (first-run setup), the picker
  // replaced the host — restore it with the pick instead of writing to a gone input.
  _fbCallback = (picked) => { closeModal(); if (reg.reopen) reg.reopen(picked); else _ffApply(id, picked); };
  showModal('<div class="modal-title">Select Folder</div><div class="fb-loading">Loading…</div>');
  _fbNav(start);
}

async function _ffCommit(id) {   // input blur / Enter → save what's typed
  await _ffApply(id, (document.getElementById('ff-' + id)?.value || '').trim());
}

async function _ffApply(id, path) {
  const reg = _ffReg[id];
  if (!reg) return;
  const inp = document.getElementById('ff-' + id);
  if (inp) inp.value = path;
  const f = document.querySelector(`.folder-field[data-ffid="${id}"]`);
  if (f) f.classList.toggle('empty', !path);
  try {
    await reg.save(path);
    if (reg.whisper) {
      const w = document.getElementById('ffw-' + id);
      if (w) { w.classList.add('show'); setTimeout(() => w.classList.remove('show'), 1600); }
    }
  } catch (e) {
    showToast('Folder update failed'); console.error(e);
  }
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

function _fbSelect() {   // "Select This Folder" — hands the current dir to the field's callback
  const cb = _fbCallback;
  _fbCallback = null;
  if (cb) cb(_fbPath); else closeModal();
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
        btn.title = _stateLabel(st);
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
      // Guard the cache mutation with onView: _detailSeries lingers after you
      // leave a series, and matching on issue NUMBER alone would flip the
      // last-viewed series' #N owned when a pull-list download of another
      // series' #N lands.
      const obj = onView ? _detailSeries?.issues?.find(i => i.number === number) : null;
      if (obj) obj.owned = 1;
      const tile = onView ? document.querySelector(`.issue-tile[data-num="${number}"]`) : null;
      if (tile && obj && _detailSeries) tile.outerHTML = _issueTileHtml(_detailSeries, obj);
      // Pull-list rows aren't .issue-tile — their ↓ button was left frozen at
      // the last percent forever on success (the failure branch below resets
      // it; this is the success-side twin). Flip it to a done check.
      else if (btn) { btn.textContent = '✓'; btn.title = 'Downloaded'; btn.disabled = true; }
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

function confirmDelete(id) {
  // Title comes from _detailSeries, NOT an onclick string param — interpolating
  // a title into inline JS breaks on apostrophes (HTML entities decode back to
  // raw quotes before the JS engine parses the handler, so esc() can't save you).
  // One Remove, one meaning (2026-10-08): the series AND its files. 'Untrack'
  // is gone — a folder left on disk just comes back on the next shelf scan.
  // Same modal from the series page, its match banner, and a Needs matching row.
  const s = (_detailSeries && _detailSeries.id === id) ? _detailSeries : (_nmRows || []).find(x => x.id === id) || null;
  const title = s?.title || 'this series';
  const files = s?.issues ? s.issues.filter(i => i.owned).length : (s?.owned ?? 0);
  const hasFolder = !!s?.folder_path;
  showModal(`
    <div class="modal-title">Remove Series</div>
    <div class="confirm-body">
      Remove <strong style="color:var(--tp)">${esc(title)}</strong>${hasFolder ? ` and its folder${files ? ` (${files} file${files === 1 ? '' : 's'})` : ''}` : ''}?
      <div class="confirm-note">${hasFolder
        ? 'The folder moves to the bin and is deleted for good after 7 days — Undo on the toast until then. Komga will lose these books on its next scan. To stop fetching a series but keep what you have, turn its pull list off instead.'
        : 'Nothing on disk belongs to it; it’s simply forgotten.'}</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="doDelete(${id})">Remove</button>
    </div>
  `);
}

async function doDelete(id) {
  closeModal();
  let r;
  try {
    r = await api.del(`/api/series/${id}`);
  } catch (e) {
    showToast(`Remove failed: ${e.message || ''}`, 'error'); console.error(e); return;
  }
  if (r?.trashed_to) showToastAction(`${r.title} removed — files in the bin for ${r.purge_days} days`, 'Undo', () => _undoTrash(r.trashed_to));
  else showToast(`${r?.title || 'Series'} removed`);
  if (currentView === 'needs-match') {
    // stay on the list: drop the row, keep the count honest, keep your search
    const row = document.getElementById(`nm-${id}`);
    if (row) row.remove();
    _nmRows = (_nmRows || []).filter(x => x.id !== id);
    _updateNeedsBadge(_nmRows.length);
    if (!_nmRows.length) renderNeedsMatch();
    return;
  }
  navigate(_detailSeries && (_detailSeries.match_status === 'pending' || _detailSeries.match_status === 'needs_match') ? 'needs-match' : 'library');
}

// --- Pull List ---

function _pullStatus(e) {
  const lt = _localToday(), ut = _usToday();
  if (e.owned) return `<span class="pull-status pull-status-owned">✓</span>`;
  if (e.ignored) return `<span class="pull-status pull-status-ignored">Ignored</span>`;
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
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Pull List';
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-sm ${_pullShowPast ? 'btn-primary' : 'btn-ghost'}"
      onclick="_togglePullPast(this)">+ Past 4 Weeks</button>
  `;
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
    setApp('<div class="state-msg">Nothing on your pull list.</div>');
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
              <div class="pull-thumb-wrap"><img class="pull-thumb" src="/api/series/${sid}/issues/${e.number}/thumbnail" alt=""
                loading="lazy" onerror="this.src='/api/series/${sid}/thumbnail';this.onerror=null"></div>
              <div class="pull-meta">
                <div class="pull-series u-truncate">${esc(e.title)}</div>
                <div class="pull-issue">#${fmtNum(e.number)}</div>
              </div>
              ${_pullStatus(e)}
              <span class="pull-act">${(!e.owned && e.store_date < _usToday())
                ? `<button class="pull-dl" data-dl="${sid}:${e.number}" title="Download this issue" aria-label="Download issue ${fmtNum(e.number)}"
                     onclick="event.stopPropagation(); pullDownload(${sid}, ${e.number}, this)">↓</button>`
                : ''}</span>
            </div>
          `;
        }).join('')}
      </div>
    `).join('');

  setApp(html);
}

// --- Activity / Queue ---

// THE queue-state table: chip class + human label, one row per state. The
// Activity chips and the issue-tile tooltip both read from here — there were
// two drifting copies of this mapping before.
const QUEUE_STATE = {
  proposed:        ['chip chip-upcoming', 'Confirm?'],
  queued:          ['chip chip-muted',  'Queued'],
  searching:       ['chip chip-active', 'Searching'],
  found:           ['chip chip-muted',  'Found'],
  downloading:     ['chip chip-active', 'Downloading'],
  pending_usenet:  ['chip chip-active', 'Usenet'],
  pending_torrent: ['chip chip-active', 'Torrent'],
  processing:      ['chip chip-muted',  'Processing'],
  done:            ['chip chip-done',   'Done'],
  not_found:       ['chip chip-warn',   'Not Found'],
  failed:          ['chip chip-fail',   'Failed'],
};
const _stateLabel = st => (QUEUE_STATE[st] || [null, st])[1];
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
let _activityRemoving = false;   // true while a row/card is animating out

// States that live in the "In Progress" section; everything else is history.
const ACT_ACTIVE_STATES = ['queued','searching','found','downloading','pending_usenet','pending_torrent','processing','proposed'];

async function renderActivity() {
  clearTimeout(_activityPollTimer);
  _activitySig = null;          // force a full rebuild when entering the view
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Activity';
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-ghost btn-sm" onclick="triggerSweep(this)">Sweep Missing</button>
    <button class="btn btn-ghost btn-sm" onclick="forceQueueStart(this)">Start Queue</button>
    <button class="btn btn-ghost btn-sm" onclick="clearHistory(this)">Clear History</button>
  `;
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
  // don't. When they DO change, we reconcile row-by-row IN PLACE — never a full
  // innerHTML rebuild (that recreated every <img> and replayed the cover-in
  // animation on 20 innocent rows because one row moved). See _reconcileActivity.
  const sig = queue.map(q => `${q.id}:${q.state}`).join('|');
  if (sig === _activitySig) {
    queue.forEach(q => {
      const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
      const fill = document.getElementById(`actfill-${q.id}`);
      const text = document.getElementById(`acttext-${q.id}`);
      if (fill) fill.style.width = `${pct}%`;
      if (text) text.textContent = `${pct}%${_actProgressDetail(q)}`;
      // The engine name flips mid-search (GetComics → Usenet → torrent); a bare
      // textContent swap reads as a flash, so crossfade it instead.
      const ss = document.getElementById(`actsearch-${q.id}`);
      if (ss && q.search_status) _softText(ss, q.search_status);
    });
  } else {
    _activitySig = sig;
    await _reconcileActivity(queue);
  }
  const hasActive = queue.some(q => ['searching','found','downloading','processing','pending_usenet','pending_torrent'].includes(q.state));
  // Within the post-retry window, keep polling while anything's still queued so
  // we don't freeze on the stale `queued` card and miss the real outcome.
  const pumping = Date.now() < _activityPumpUntil && queue.some(q => q.state === 'queued');
  if (hasActive || pumping) _activityPollTimer = setTimeout(_refreshActivity, 2000);
}

function _actChip(state) {
  const [cls, label] = QUEUE_STATE[state] || ['chip chip-muted', state];
  return `<span class="${cls}">${label}</span>`;
}

// One line of progress detail under a downloading row. Usenet progress is a
// percentage from SAB (no byte counts); GetComics has bytes. Returns RAW text —
// the innerHTML builder esc()s it, the textContent patcher must not.
function _actProgressDetail(q) {
  return q.state === 'pending_usenet'
    ? ' · Usenet'
    : q.state === 'pending_torrent'
    ? ' · Torrent' + (q.search_status ? ' · ' + q.search_status : '')
    : (q.progress ? ' — ' + _fmtBytes(q.progress.done) + ' / ' + _fmtBytes(q.progress.total) : '');
}

// Activity rows are kind-agnostic: a trade shows "Vol N" and the series cover
// (no per-issue thumbnail), an issue shows "#N" and its own.
function _actLabel(q) {
  if (q.kind === 'trade') {
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return _volLabel(m, 'Trade');
  }
  return `#${fmtNum(q.issue_number)}`;
}
function _actThumb(q) {
  if (!q.tracked_series_id) return '';
  const seriesThumb = `/api/series/${q.tracked_series_id}/thumbnail`;
  if (q.kind === 'trade') {
    // A proposal shows the cover of what actually came back; otherwise the
    // trade's own LOCG cover (stashed in meta), falling back to the series.
    if (q.state === 'proposed') return `<img src="/api/queue/${q.id}/proposal-cover" alt="" onerror="this.src='${seriesThumb}'">`;
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return `<img src="${esc(m.cover || seriesThumb)}" alt="" onerror="this.src='${seriesThumb}'">`;
  }
  return `<img src="/api/series/${q.tracked_series_id}/issues/${q.issue_number}/thumbnail" alt="" onerror="this.src='${seriesThumb}'">`;
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
  if (/already exists|duplicate/i.test(e))            return 'A copy is already on the shelf';
  if (/No folder set/i.test(e))                       return 'No comics folder set for this series';
  return strip(e);                                                                   // fall back to the raw reason
}

// The variable sub-block under the title line: search status while searching, a
// progress bar while downloading, the plain-language reason once it's history.
function _actSubHtml(q) {
  if (q.state === 'searching') return `
        <div class="act-row-progress">
          <div class="act-progress-text" id="actsearch-${q.id}">${esc(q.search_status || 'Searching…')}</div>
        </div>`;
  if (['downloading','pending_usenet','pending_torrent'].includes(q.state)) {
    const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
    return `
        <div class="act-row-progress">
          <div class="act-progress-track"><div class="act-progress-fill" id="actfill-${q.id}" style="width:${pct}%"></div></div>
          <div class="act-progress-text" id="acttext-${q.id}">${pct}%${esc(_actProgressDetail(q))}</div>
        </div>`;
  }
  if (q.state === 'proposed') {
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    const f = (m.proposal && m.proposal.files) || [];
    const pages = f.reduce((a, x) => a + (x.pages || 0), 0), mb = Math.round(f.reduce((a, x) => a + (x.size || 0), 0) / 1048576);
    const name = f.length ? f[0].path.split('/').pop() : '';
    return `<div class="act-row-reason" style="color:var(--amb)">Found by name, not verified — ${f.length} file${f.length === 1 ? '' : 's'}, ${pages} pages, ${mb} MB${name ? ' · ' + esc(name) : ''}. Is this it?</div>`;
  }
  const reason = _actReason(q);
  return reason ? `<div class="act-row-reason">${esc(reason)}</div>` : '';
}

// Chip + buttons cluster for a row's current state.
function _actActionsHtml(q) {
  let btns = '';
  if (q.state === 'proposed') {
    btns = `
            <button class="btn btn-ghost btn-sm" onclick="proposalDecide(${q.id}, 'reject', this)" title="Not it — bin it and remember the release">Reject</button>
            <button class="btn btn-primary btn-sm" onclick="proposalDecide(${q.id}, 'confirm', this)" title="Place it on the shelf">Confirm</button>`;
    return `${_actChip(q.state)}${btns}`;
  }
  if (q.state === 'queued') {
    // Queued items can stall on a retry_after backoff (dupe guard). Give the
    // user the wheel: kick a search right now, or yank it from the queue.
    btns = `
            <button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)" title="Search GetComics/Usenet now (skip backoff)">Search now</button>
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from queue" aria-label="Remove from queue">✕</button>`;
  } else if (!ACT_ACTIVE_STATES.includes(q.state)) {
    const retry = q.state !== 'done' ? `<button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)">Retry</button>` : '';
    btns = `
            ${retry}
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from history" aria-label="Remove from history">✕</button>`;
  }
  return `${_actChip(q.state)}${btns}`;
}

async function proposalDecide(qid, action, btn) {
  btn.disabled = true;
  try {
    const r = await api.post(`/api/queue/${qid}/${action}`, {});
    showToast(action === 'confirm' ? `Placed${r.placed ? ` — ${r.placed.length} file${r.placed.length === 1 ? '' : 's'}` : ''}` : 'Rejected — the next search tries something else');
    renderActivity();
  } catch (e) { btn.disabled = false; showToast(`Couldn’t ${action}`, 'error'); }
}

function _actRowHtml(q) {
  const active = ACT_ACTIVE_STATES.includes(q.state);
  const errTip = q.error ? ` title="${esc(q.error)}"` : '';
  const nav = q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
  return `
        <div class="act-row${q.state === 'done' ? ' done' : ''}" data-qid="${q.id}" data-qstate="${q.state}"${errTip}>
          <div class="act-row-cover">${_actThumb(q)}</div>
          <div class="act-row-meta"${nav}>
            <div class="act-row-title u-truncate">${esc(q.title)}</div>
            <div class="act-row-issue">${active && q.publisher ? esc(q.publisher) + ' · ' : ''}${_actLabel(q)}</div>
            ${_actSubHtml(q)}
          </div>
          <div class="act-row-actions">${_actActionsHtml(q)}</div>
        </div>`;
}

function _actRowNode(q) {
  const t = document.createElement('template');
  t.innerHTML = _actRowHtml(q).trim();
  return t.content.firstElementChild;
}

function _buildActivityHtml(queue) {
  const inProgress = queue.filter(q => ACT_ACTIVE_STATES.includes(q.state));
  const completed  = queue.filter(q => !ACT_ACTIVE_STATES.includes(q.state));

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
    html += `<div class="act-section" data-sec="progress">
      <div class="act-section-hdr">In Progress</div>
      ${inProgress.map(_actRowHtml).join('')}
    </div>`;
  }
  if (completed.length) {
    html += `<div class="act-section" data-sec="completed">
      ${completed.map(_actRowHtml).join('')}
    </div>`;
  }
  html += '</div>';
  setApp(html);
}

// Crossfade a text swap instead of hard-cutting it. No-op when nothing changed.
function _softText(el, text) {
  if (el.textContent === text) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) { el.textContent = text; return; }
  el.style.transition = `opacity var(--t-fast) ease`;
  el.style.opacity = '0';
  setTimeout(() => { el.textContent = text; el.style.opacity = '1'; }, 160);
}

// Grow a freshly inserted row open (the mirror of _animateRowOut): measure its
// natural height, pin it shut, then transition open and unpin.
function _animateRowIn(el) {
  return new Promise(resolve => {
    if (!el) { resolve(); return; }
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) { resolve(); return; }
    const h = el.offsetHeight;               // natural height, measured pre-collapse
    el.classList.add('act-adding');          // collapsed start state (no transition yet)
    void el.offsetHeight;                    // commit the collapsed frame
    el.classList.add('act-anim');
    el.classList.remove('act-adding');
    el.style.maxHeight = `${h}px`;
    let done = false;
    const finish = () => {
      if (done) return; done = true;
      el.classList.remove('act-anim');
      el.style.maxHeight = '';
      resolve();
    };
    el.addEventListener('transitionend', ev => { if (ev.propertyName === 'max-height') finish(); });
    setTimeout(finish, 360);                 // belt-and-suspenders, same as row-out
  });
}

// A row changed state but stayed in its section: morph it in place. The row node
// and its <img> survive untouched — that's the whole point (recreating the img is
// what replayed the cover-in animation and flashed).
function _morphActRow(el, q) {
  el.dataset.qstate = q.state;
  el.classList.toggle('done', q.state === 'done');
  if (q.error) el.setAttribute('title', q.error); else el.removeAttribute('title');
  const actions = el.querySelector('.act-row-actions');
  if (actions) {
    actions.innerHTML = _actActionsHtml(q);
    actions.classList.remove('act-fade-in');
    void actions.offsetWidth;                // restart the fade even on back-to-back morphs
    actions.classList.add('act-fade-in');
  }
  // Swap the sub-block (search line / progress bar / failure reason); the
  // replacement animates open via the act-sub-in keyframe in CSS.
  el.querySelectorAll('.act-row-progress, .act-row-reason').forEach(n => n.remove());
  const sub = _actSubHtml(q);
  const meta = el.querySelector('.act-row-meta');
  if (sub && meta) {
    meta.insertAdjacentHTML('beforeend', sub);
    meta.lastElementChild.classList.add('act-sub-in');   // grow open, don't shove
  }
}

// Keyed row-by-row reconcile: removed rows collapse out (neighbors slide up on the
// same transition), new rows grow in, state changes morph the existing node, and a
// section hop (In Progress → history) is a collapse on one side + a grow on the
// other. Nothing here ever rebuilds an untouched row.
async function _reconcileActivity(queue) {
  const wrap = document.querySelector('.act-wrap');
  if (!wrap) { _buildActivityHtml(queue); return; }

  _activityRemoving = true;   // hold off re-entrant polls while rows are mid-flight
  try {
    if (!queue.length) {
      const rows = Array.from(wrap.querySelectorAll('.act-row'));
      rows.forEach((row, i) => { row.style.transitionDelay = `${i * 30}ms`; });
      await Promise.all(rows.map(_animateRowOut));
      _buildActivityHtml(queue);   // the empty state
      return;
    }

    const want = {
      progress:  queue.filter(q => ACT_ACTIVE_STATES.includes(q.state)),
      completed: queue.filter(q => !ACT_ACTIVE_STATES.includes(q.state)),
    };
    const wantIds = new Set(queue.map(q => String(q.id)));
    const anims = [];

    // Rows that left the queue entirely.
    wrap.querySelectorAll('.act-row[data-qid]').forEach(el => {
      if (!wantIds.has(el.dataset.qid) && !el.classList.contains('act-removing')) anims.push(_animateRowOut(el));
    });

    for (const sec of ['progress', 'completed']) {
      const items = want[sec];
      if (!items.length) continue;   // empty sections are swept after the animations settle
      let secEl = wrap.querySelector(`.act-section[data-sec="${sec}"]`);
      if (!secEl) {
        const t = document.createElement('template');
        t.innerHTML = sec === 'progress'
          ? '<div class="act-section act-fade-in" data-sec="progress"><div class="act-section-hdr">In Progress</div></div>'
          : '<div class="act-section act-fade-in" data-sec="completed"></div>';
        secEl = t.content.firstElementChild;
        sec === 'progress' ? wrap.prepend(secEl) : wrap.append(secEl);
      }
      const hdr = secEl.querySelector('.act-section-hdr');
      let anchor = null;   // the last row we placed; the next one goes right after it
      for (const q of items) {
        let el = wrap.querySelector(`.act-row[data-qid="${q.id}"]:not(.act-removing)`);
        if (el && el.closest('.act-section') !== secEl) {
          anims.push(_animateRowOut(el));   // hopped sections: collapse the old side...
          el = null;                        // ...and grow a fresh row on the new side
        }
        if (!el) {
          el = _actRowNode(q);
          secEl.insertBefore(el, anchor ? anchor.nextSibling : (hdr ? hdr.nextSibling : secEl.firstChild));
          anims.push(_animateRowIn(el));
        } else {
          if (el.dataset.qstate !== q.state) _morphActRow(el, q);
          // Keep DOM order in step with the queue (skipping rows mid-collapse).
          let prev = el.previousElementSibling;
          while (prev && prev.classList.contains('act-removing')) prev = prev.previousElementSibling;
          if (prev !== (anchor || hdr)) secEl.insertBefore(el, anchor ? anchor.nextSibling : (hdr ? hdr.nextSibling : secEl.firstChild));
        }
        anchor = el;
      }
    }

    await Promise.all(anims);
    // Sections left with no rows (header dangling): collapse them away too.
    wrap.querySelectorAll('.act-section').forEach(sec => {
      if (!sec.querySelector('.act-row')) anims.push(_animateRowOut(sec));
    });
    await Promise.all(anims);
  } finally {
    _activityRemoving = false;
  }
}

async function triggerSweep(btn) {
  btn.disabled = true; btn.textContent = 'Sweeping…';
  try {
    await api.post('/api/queue/sweep', {});
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Sweep Missing';
    showToast('Sweep failed'); console.error(e); return;
  }
  btn.textContent = 'Queued ✓';
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Sweep Missing'; renderActivity(); }, 1500);
}

async function forceQueueStart(btn) {
  btn.disabled = true; btn.textContent = 'Starting…';
  try {
    await api.post('/api/queue/process', {});
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Start Queue';
    showToast('Queue start failed'); console.error(e); return;
  }
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
  try {
    await api.post('/api/queue/clear-history', {});
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Clear History';
    showToast('Clear failed'); console.error(e); return;
  }
  // Fade the completed rows out in place — don't rebuild the whole list (the in-progress
  // section would flash). Stagger them a touch for a tidy cascade, then drop the sig so
  // the next poll reconciles cleanly.
  _activityRemoving = true;
  const rows = Array.from(document.querySelectorAll('.act-section[data-sec="completed"] .act-row'));
  rows.forEach((row, i) => { row.style.transitionDelay = `${i * 30}ms`; });
  await Promise.all(rows.map(_animateRowOut));
  // The Completed section header is now stranded with no rows — yank it too.
  document.querySelectorAll('.act-section').forEach(sec => {
    if (!sec.querySelector('.act-row')) sec.remove();
  });
  _activitySig = null;
  _activityRemoving = false;
  btn.disabled = false; btn.textContent = 'Clear History';
  _refreshActivity();
}

async function retryQueue(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Searching…';
  const r = await api.post(`/api/queue/${id}/retry`, {});
  if (r && r.busy) {
    // the worker is mid-download; the ask is remembered and runs when it's free
    btn.textContent = 'Next up';
    showToast('The downloader is busy with another item — this one runs the moment it’s free');
  }
  // Backend runs the search on a thread — item stays `queued` for a beat, then
  // flips to searching → done/not_found. Pump the poll loop so the UI rides
  // that transition out instead of going dead on the click (longer when busy).
  _activityPumpUntil = Date.now() + (r && r.busy ? 180000 : 30000);
  _activitySig = null;   // force a rebuild on the next poll even if state lags
  _refreshActivity();
}

async function removeQueue(id, btn) {
  btn.disabled = true;
  await api.del(`/api/queue/${id}`);
  // Animate ONLY this row out — don't rebuild the list (that flashes every cover).
  // Drop the sig so the next poll rebuilds from scratch without trying to re-add the
  // row we just collapsed.
  const el = btn.closest('.act-row');
  _activitySig = null;
  _activityRemoving = true;
  await _animateRowOut(el);
  // If that emptied a section (header left dangling), clear the stragglers.
  document.querySelectorAll('.act-section').forEach(sec => {
    if (!sec.querySelector('.act-row')) sec.remove();
  });
  _activityRemoving = false;
  if (!document.querySelector('.act-row')) _refreshActivity();
}

// --- Settings ---

async function renderSettings() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Settings';
  setApp('<div class="state-msg">Loading...</div>');

  const cfg = await api.get('/api/config');
  const komgaCfg  = !!(cfg.komga_url && cfg.komga_user);

  setApp(`
    <div class="settings-grid">
      <div>
        <div class="settings-card">
          ${_settingsHeader('Comics Library', 'required', 'library')}
          <div class="settings-field">
            <label class="settings-field-label u-label" for="ff-root">Library path</label>
            ${folderField('root', cfg.comics_root, 'fs', async (path) => {
              const c = await api.patch('/api/config', { comics_root: path });
              _appConfig = { ..._appConfig, comics_root: path, comics_root_ok: c.comics_root_ok };
              _updateRootStatus(c.comics_root_ok);
            })}
            <div class="settings-help" id="root-status"></div>
          </div>
        </div>
        <div class="settings-card" style="margin-top:32px">
          ${_settingsHeader('Sync Schedule', '', 'schedule')}
          ${_settingsField('f-sync-hours', 'Hours (24h, comma-separated)', cfg.sync_hours)}
        </div>
        <div class="settings-section ${cfg.prowlarr_enabled ? '' : 'section-off'}" id="sec-prowlarr" style="margin-top:32px">
          ${_settingsSectionHead('Prowlarr', 'engine', 't-prowlarr', cfg.prowlarr_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-prowlarr-url', 'Server URL', cfg.prowlarr_url, { ph: 'http://host:9696', test: { cardId: 'prowlarr', configured: cfg.prowlarr_configured } })}
              ${_settingsField('f-prowlarr-apikey', 'API Key', '', { set: cfg.prowlarr_configured, ph: 'Enter API key' })}
            </div>
            <div class="settings-subsections">
              <div class="settings-section ${cfg.usenet_enabled ? '' : 'section-off'}" id="sec-usenet">
                ${_settingsSectionHead('Usenet', 'client', 't-usenet', cfg.usenet_enabled)}
                <div class="settings-section-body"><div class="settings-section-inner">
                  <div class="settings-card">
                    ${_settingsField('f-sab-url', 'Server URL', cfg.sab_url, { ph: 'http://host:8080', test: { cardId: 'sabnzbd', configured: cfg.sab_configured } })}
                    ${_settingsField('f-sab-apikey', 'API Key', '', { set: cfg.sab_configured, ph: 'Enter API key' })}
                  </div>
                </div></div>
              </div>
              <div class="settings-section ${cfg.torrent_enabled ? '' : 'section-off'}" id="sec-torrent">
                ${_settingsSectionHead('Torrent', 'client', 't-torrent', cfg.torrent_enabled)}
                <div class="settings-section-body"><div class="settings-section-inner">
                  <div class="settings-card">
                    ${_settingsField('f-qbit-url', 'Server URL', cfg.qbit_url, { ph: 'http://host:8090', test: { cardId: 'qbit', configured: cfg.qbit_configured } })}
                    ${_settingsField('f-qbit-user', 'Username', cfg.qbit_user)}
                    ${_settingsField('f-qbit-pass', 'Password', '', { set: cfg.qbit_configured, ph: 'Enter password' })}
                  </div>
                </div></div>
              </div>
            </div>
          </div></div>
        </div>
      </div>
      <div>
        <div class="settings-section ${cfg.komga_enabled ? '' : 'section-off'}" id="sec-komga">
          ${_settingsSectionHead('Komga', 'reader', 't-komga', cfg.komga_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-komga-url', 'Server URL', cfg.komga_url, { test: { cardId: 'komga', configured: komgaCfg } })}
              ${_settingsField('f-komga-user', 'Username', cfg.komga_user)}
              ${_settingsField('f-komga-pass', 'Password', '', { set: komgaCfg })}
              ${_settingsField('f-komga-lib', 'Library ID', cfg.komga_library_id)}
            </div>
          </div></div>
        </div>
        <div class="settings-section ${cfg.comicvine_enabled ? '' : 'section-off'}" id="sec-comicvine" style="margin-top:32px">
          ${_settingsSectionHead('ComicVine', 'storylines', 't-comicvine', cfg.comicvine_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-cv-apikey', 'API Key', '', { set: cfg.comicvine_configured, ph: 'Enter API key', test: { cardId: 'comicvine', configured: cfg.comicvine_configured } })}
            </div>
          </div></div>
        </div>
        <div class="settings-card" style="margin-top:32px">
          ${_settingsHeader('League of Comic Geeks', 'your browser’s pass', 'locg', true, cfg.locg_cf_configured)}
          ${_settingsField('f-locg-cf', 'cf_clearance cookie', '', { set: cfg.locg_cf_configured, ph: 'Paste from DevTools → Application → Cookies' })}
          <div class="settings-help">${cfg.locg_cf_configured
            ? `Pass saved ${esc((cfg.locg_cf_set_at || '').slice(0, 16))} UTC from ${esc(_browserName(cfg.locg_user_agent))}. It lasts a few hours and is forgotten the moment LOCG refuses it. Paste from the browser that has LOCG open.`
            : 'LOCG challenges anything that isn’t a browser. When yours gets through, its cookie lets Kometa ride the same session — same house, same IP — at the usual trickle. Optional; Metron covers nearly everything now.'}</div>
        </div>
      </div>
    </div>
  `);
  _updateRootStatus(cfg.comics_root_ok);
  // Sections that load already-off start collapsed WITHOUT animation (no
  // fold-in flash on entering Settings). Prowlarr is the master — off folds the
  // whole usenet+torrent branch it wraps.
  if (!cfg.komga_enabled)     _collapseSection(document.getElementById('sec-komga'), true, false);
  if (!cfg.comicvine_enabled) _collapseSection(document.getElementById('sec-comicvine'), true, false);
  if (!cfg.prowlarr_enabled)  _collapseSection(document.getElementById('sec-prowlarr'), true, false);
  if (!cfg.usenet_enabled)   _collapseSection(document.getElementById('sec-usenet'), true, false);
  if (!cfg.torrent_enabled)  _collapseSection(document.getElementById('sec-torrent'), true, false);
}

// Autosave architecture: every field persists itself on change — no Save
// button to forget, no dirty form to lose to a stray navigation. Secrets save
// on blur and never round-trip back into the DOM.
// input id → config key, owning card (for the 'saved' whisper), and which
// integration to re-verify after the value changes.
const _SETTINGS_FIELDS = {
  'f-komga-url':   { card: 'komga',     key: 'komga_url',        test: 'komga' },
  'f-komga-user':  { card: 'komga',     key: 'komga_user',       test: 'komga' },
  'f-komga-pass':  { card: 'komga',     key: 'komga_pass',       test: 'komga', secret: true },
  'f-komga-lib':   { card: 'komga',     key: 'komga_library_id' },
  'f-sync-hours':  { card: 'schedule',  key: 'sync_hours' },
  'f-sab-url':     { card: 'sabnzbd',   key: 'sab_url',          test: 'sabnzbd' },
  'f-sab-apikey':  { card: 'sabnzbd',   key: 'sab_apikey',       test: 'sabnzbd', secret: true },
  'f-qbit-url':        { card: 'qbit',     key: 'qbit_url',         test: 'qbit' },
  'f-qbit-user':       { card: 'qbit',     key: 'qbit_user',        test: 'qbit' },
  'f-qbit-pass':       { card: 'qbit',     key: 'qbit_pass',        test: 'qbit', secret: true },
  'f-prowlarr-url':    { card: 'prowlarr', key: 'prowlarr_url',     test: 'prowlarr' },
  'f-prowlarr-apikey': { card: 'prowlarr', key: 'prowlarr_apikey',  test: 'prowlarr', secret: true },
  'f-cv-apikey':       { card: 'comicvine', key: 'cv_api_key',      test: 'comicvine', secret: true },
  'f-locg-cf':         { card: 'locg',      key: 'locg_cf_clearance', test: 'locg', secret: 'mask' },
};

const _TEST_ENDPOINTS = { komga: 'komga', sabnzbd: 'sab', qbit: 'qbit', prowlarr: 'prowlarr', comicvine: 'comicvine', locg: 'locg' };

// 'Chrome 154 on macOS' from a User-Agent — for the LOCG card's status line.
function _browserName(ua) {
  ua = ua || '';
  const os = /Macintosh/.test(ua) ? 'macOS' : /Windows/.test(ua) ? 'Windows' : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : '';
  const m = ua.match(/(Edg|Chrome|Firefox|Version)\/(\d+)/);
  const name = !m ? 'a browser' : m[1] === 'Edg' ? `Edge ${m[2]}` : m[1] === 'Version' ? `Safari ${m[2]}` : `${m[1]} ${m[2]}`;
  return os ? `${name} on ${os}` : name;
}

function _settingsField(id, label, value, opts = {}) {
  const f = _SETTINGS_FIELDS[id] || {};
  const secret = !!f.secret;
  const ph = secret
    ? (opts.set ? 'Leave blank to keep current' : (opts.ph || ''))
    : (opts.ph || '');
  const labelRow = opts.test
    ? `<div class="settings-field-hdr">
        <label class="settings-field-label u-label" for="${id}">${label}</label>
        ${_testControls(opts.test.cardId, opts.test.configured)}
      </div>`
    : `<label class="settings-field-label u-label" for="${id}">${label}</label>`;
  // secret: 'mask' — hidden on screen like a password, but a TEXT input: a
  // pasted cookie is not a password, and type=password had Safari offering to
  // invent a strong one for it. The data-* attrs wave off 1Password/LastPass.
  const mask = f.secret === 'mask';
  return `
    <div class="settings-field">
      ${labelRow}
      <input class="settings-input${mask ? ' settings-input-mask' : ''}" id="${id}"
        type="${secret && !mask ? 'password' : 'text'}"
        value="${esc(value || '')}" data-last="${esc(value || '')}"
        placeholder="${esc(ph)}"
        autocomplete="${secret && !mask ? 'new-password' : 'off'}" spellcheck="false"
        ${mask ? 'data-1p-ignore data-lpignore="true" data-form-type="other" autocorrect="off" autocapitalize="off"' : ''}
        onchange="_settingsChanged(this)">
    </div>`;
}

// A pill toggle switch — track + thumb, matches kometa.pen (mEkWZ/eLfqf).
// role="switch" + keyboard come free from the native checkbox.
function _settingsToggle(id, on, label) {
  return `<label class="toggle-switch">
    <input type="checkbox" id="${id}" role="switch" ${on ? 'checked' : ''}
      aria-label="${esc(label)}" onchange="_toggleSource(this)">
    <span class="toggle-track"><span class="toggle-thumb"></span></span>
  </label>`;
}

// Verify cluster for an integration — status dot + test + disconnect. Lives on
// the Server URL field's label row (it's the connection you're testing), not in
// the section header, which stays just title + tag + toggle.
function _testControls(cardId, configured) {
  return `<span class="settings-head-right">
        <span class="settings-whisper" id="sw-${cardId}"></span>
        <span class="settings-dot ${configured ? 'cfg' : ''}" id="dot-${cardId}"
          title="${configured ? 'configured — not yet verified' : 'not configured'}"></span>
        <button class="btn-link" onclick="testIntegration('${cardId}')">test</button>
        <button class="btn-link" id="dc-${cardId}" style="${configured ? '' : 'display:none'}"
          onclick="disconnectIntegration('${cardId}', this)">disconnect</button>
      </span>`;
}

// One header row per toggleable section (Komga / Prowlarr / Usenet / Torrent):
// title + role tag + the enable toggle, underlined. Off folds the body; head stays.
function _settingsSectionHead(title, tag, toggleId, on) {
  return `<div class="settings-section-head">
    <span class="settings-section-title">${esc(title)}${tag ? ` <span class="settings-opt">${tag}</span>` : ''}</span>
    ${_settingsToggle(toggleId, on, `${title} ${on ? 'enabled' : 'disabled'}`)}
  </div>`;
}

// Toggle → persist the flag + fold/unfold the section. Backend reads the flag
// (gates the search cascade for usenet/torrent, sources.komga() for komga).
const _SOURCE_TOGGLES = {
  't-komga':     { key: 'komga_enabled',     section: 'sec-komga',     label: 'Komga' },
  't-comicvine': { key: 'comicvine_enabled', section: 'sec-comicvine', label: 'ComicVine' },
  't-prowlarr':  { key: 'prowlarr_enabled',  section: 'sec-prowlarr',  label: 'Prowlarr' },
  't-usenet':    { key: 'usenet_enabled',    section: 'sec-usenet',    label: 'Usenet' },
  't-torrent':   { key: 'torrent_enabled',   section: 'sec-torrent',   label: 'Torrents' },
};
// Fade + fold a section's body, mirroring _animateRowOut's convention: pin
// max-height to the measured height, reflow, then transition to/from 0 so it
// animates FROM a real value (max-height from an oversized guess stalls the
// first half of the fold). After an OPEN settles, release max-height so the
// cards can still grow (indexer rows added, etc.).
function _collapseSection(sec, collapsed, animate) {
  const body = sec?.querySelector('.settings-section-body');
  if (!body) return;
  sec.classList.toggle('section-off', collapsed);
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!animate || reduce) {
    body.style.maxHeight = collapsed ? '0px' : 'none';
    body.style.opacity = collapsed ? '0' : '1';
    return;
  }
  if (collapsed) {
    body.style.maxHeight = body.scrollHeight + 'px';   // pin FROM current
    void body.offsetHeight;                            // reflow so 0 transitions
    body.style.maxHeight = '0px';
    body.style.opacity = '0';
  } else {
    body.style.opacity = '1';
    body.style.maxHeight = body.scrollHeight + 'px';   // animate 0 → measured
    body.addEventListener('transitionend', function done(e) {
      if (e.propertyName !== 'max-height') return;
      body.style.maxHeight = 'none';                   // release so later content fits
      body.removeEventListener('transitionend', done);
    });
  }
}

async function _toggleSource(el) {
  const t = _SOURCE_TOGGLES[el.id];
  if (!t) return;
  const on = el.checked;
  const sec = document.getElementById(t.section);
  // Optimistic: fold THE MOMENT you tap — the save rides behind it, like every
  // other field's autosave. Waiting on the network before animating is what
  // made the fold look broken (it lagged the toggle, then snapped).
  _collapseSection(sec, !on, true);
  try {
    _appConfig = await api.patch('/api/config', { [t.key]: on ? '1' : '0' });
    showToast(`${t.label} ${on ? 'enabled' : 'disabled'}`);
  } catch (e) {
    el.checked = !on;                  // revert toggle + fold
    _collapseSection(sec, on, true);
    showToast(`${t.label} toggle failed`, 'error');
  }
}

function _settingsHeader(title, tag, cardId, integ = false, configured = false) {
  return `
    <div class="settings-card-header">
      <span>${title}${tag ? ` <span class="settings-opt">${tag}</span>` : ''}</span>
      <span class="settings-head-right">
        <span class="settings-whisper" id="sw-${cardId}"></span>
        ${integ ? `
          <span class="settings-dot ${configured ? 'cfg' : ''}" id="dot-${cardId}"
            title="${configured ? 'configured — not yet verified' : 'not configured'}"></span>
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

  const body = { [f.key]: val };
  if (f.key === 'locg_cf_clearance') {
    // the pass is bound to the browser it was issued to — if you're pasting it
    // from that same browser (the normal case), its UA is right here
    // ALWAYS the browser you're pasting from — a stale Safari string left in the
    // field sent a Chrome cookie with a Safari identity, refused every time
    // (2026-10-08 19:15). Edit the field afterwards if you really mean to.
    const ua = document.getElementById('f-locg-ua');
    body.locg_user_agent = navigator.userAgent;
    if (ua) { ua.value = navigator.userAgent; ua.dataset.last = navigator.userAgent; }
  }
  try {
    const cfg = await api.patch('/api/config', body);
    // Settings autosave changes what the SERVER knows, but every render decision
    // gated on _appConfig (Arcs tab, "Open in Komga" links, …) was reading the
    // STALE snapshot from boot() until a hard reload — a toggle could report
    // 'enabled' via toast while the UI it unlocks stayed invisible all session.
    _appConfig = cfg;
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
  // No news is good news — only surface the path when it's a problem.
  el.textContent = ok ? '' : '✗ path missing or read-only';
  el.className = ok ? 'settings-help' : 'settings-help bad';
}

async function testIntegration(integ) {
  const dot = document.getElementById(`dot-${integ}`);
  if (dot) { dot.className = 'settings-dot testing'; dot.title = 'testing…'; }
  try {
    const res = await api.post(`/api/test/${_TEST_ENDPOINTS[integ]}`, {});
    if (res.ok) {
      if (dot) { dot.className = 'settings-dot ok'; dot.title = 'verified'; }
      const dc = document.getElementById(`dc-${integ}`);
      if (dc) dc.style.display = '';
    } else {
      if (dot) { dot.className = 'settings-dot bad'; dot.title = res.error || 'failed'; }
      showToast(`${integ}: ${res.error || 'connection failed'}`, 'error');
    }
  } catch (e) {
    if (dot) { dot.className = 'settings-dot bad'; dot.title = 'failed'; }
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

// Newznab indexer list retired — Prowlarr aggregates every indexer now, so
// there's nothing to hand-enter. (The Add/Remove UI + endpoints are gone.)

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
    // A close mid-height-tween never gets its transitionend — clear the pinned
    // inline height or the NEXT modal opens squeezed to this one's size.
    modal.style.height = '';
    modal.style.transition = '';
    backdrop.classList.add('hidden');
    backdrop.classList.remove('closing');
  }, 200);                                     // match exit duration
}

// --- Issue Detail Modal ---

let _issueVariantCovers  = [];
let _issueVariantSelected = new Set();
let _issueVariantPrimary  = null;
let _issueVariantSeriesId = null;
let _issueVariantNumber   = null;
let _issueVariantOwned    = false;  // cached at modal-open — don't re-derive via
                                     // _detailSeries.issues, which is empty for arcs

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
          <div class="issue-modal-credit-role u-label">${esc(role)}</div>
          <div class="issue-modal-credit-name">${names.map(esc).join(', ')}</div>
        </div>`).join('') + '</div>';
  }
  _animateModalHeight(() => {
    el.innerHTML = html || '<div class="state-msg" style="font-size:11px;padding:8px 0;color:var(--tq)">No details available.</div>';
    el.style.animation = 'none'; void el.offsetWidth;   // restart the fade
    el.style.animation = 'fade-in var(--t-slow) var(--ease-out)';
  });
}

async function showIssueModal(seriesId, number, opts = {}) {
  clearTimeout(_issueModalPollTimer);
  // Fast path: already viewing this series, its issues are cached on _detailSeries.
  // Called from elsewhere (an arc's cross-title reading order) — _detailSeries is
  // the ARC's payload, not this issue's own series — fetch it fresh instead of
  // silently no-op'ing, so the SAME real modal works from any context.
  let homeSeries = _detailSeries?.id === seriesId ? _detailSeries : null;
  let issue = homeSeries?.issues?.find(i => i.number === number);
  if (!issue) {
    try {
      homeSeries = await api.get(`/api/series/${seriesId}`);
    } catch (e) {
      showToast('Couldn’t load issue — ' + (e?.message || e), 'error');
      return;
    }
    issue = homeSeries?.issues?.find(i => i.number === number);
    if (!issue) return;
  }

  const st = issueStatus(issue);

  _issueVariantCovers   = [];
  _issueVariantSelected = new Set();
  _issueVariantPrimary  = null;
  _issueVariantSeriesId = seriesId;
  _issueVariantNumber   = number;
  _issueVariantOwned    = st === 'owned';

  const s = homeSeries;
  const num = `#${fmtNum(number)}`;

  // variant_cover = your saved variant pick (only present on not-yet-downloaded
  // issues); show it so an upcoming issue reflects the cover you chose. Owned issues
  // carry no pref (it's baked into the CBZ already) and fall through to Komga.
  const imgSrc = issue.variant_cover
    ? issue.variant_cover
    : issue.komga_book_id
      ? esc(bookThumb(issue))
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
    ignored: `<span class="chip chip-ignored">Ignored</span>`,
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
  if (st === 'owned') {
    // Kometa's own reader first; Komga stays as the fallback until it's retired.
    const komga = issue.komga_book_id && _appConfig.komga_url
      ? `<a class="btn btn-ghost komga-read-link" href="${komgaBase()}/book/${esc(issue.komga_book_id)}/read" target="_blank" rel="noopener">Komga</a>`
      : '';
    footerAction = `${komga}<button class="btn btn-primary" onclick="openIssueReader(${seriesId}, ${number})">Read</button>`;
    // Opened from a book card (On Deck): the same modal, with the card's
    // actions in the footer — the series behind it, Not now, read state.
    if (opts.book) {
      const b = opts.book;
      footerAction = `
        <button class="btn btn-ghost" onclick="closeModal(); navigate('series-detail', {id: ${seriesId}})">Go to series</button>
        ${b.dismissable ? `<button class="btn btn-ghost" onclick="closeModal(); _odDismiss(${b.book_id}, document.getElementById('od-${b.book_id}')?.querySelector('.od-menu'))">Not now</button>` : ''}
        <button class="btn btn-ghost" onclick="closeModal(); _bookSetRead(${b.book_id}, ${b.completed ? 'false' : 'true'})">${b.completed ? 'Mark as unread' : 'Mark as read'}</button>
        <button class="btn btn-primary" onclick="closeModal(); navigate('read', {book: ${b.book_id}})">Read</button>`;
    }
  } else if (st === 'missing' || st === 'today') {
    footerAction = `<button class="btn btn-ghost" onclick="setIssueIgnored(${seriesId}, ${number}, true)"
        title="Stop searching for this issue">Ignore</button>
      <button class="btn btn-primary" id="issue-dl-btn" onclick="issueDownload(${seriesId}, ${number})">Download</button>`;
  } else if (st === 'ignored') {
    footerAction = `<button class="btn btn-primary" onclick="setIssueIgnored(${seriesId}, ${number}, false)">Stop ignoring</button>`;
  }

  // Details + variants come from Metron OR LOCG now (kometa/issue_meta.py) —
  // either id lights the tabs up. Name kept so the template below reads as before.
  const hasLocgId = !!(issue.locg_issue_id || issue.metron_issue_id);

  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(imgSrc, num)}
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
            ${hasLocgId ? '<div class="state-msg" style="font-size:11px;padding:8px 0">Loading details…</div>' : ''}
          </div>
        </div>
        ${hasLocgId ? `
        <div class="issue-modal-panel" id="impanel-variants">
          <div id="variant-area" class="variant-loading">Loading covers…</div>
          <div id="variant-footer" class="variant-footer" style="display:none">
            <div class="variant-hint" id="variant-hint">Click a cover to view it large — include or ★ it from there.</div>
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

  // Owned: the star and the 1–5 rating, on the book behind the issue.
  if (st === 'owned') {
    const bookP = opts.book ? api.get(`/api/books/${opts.book.book_id}`) : api.get(`/api/series/${seriesId}/issues/${number}/book`);
    bookP.then(bk => {
      const host = document.getElementById('issue-modal-details');
      if (host && bk) host.insertAdjacentHTML('beforebegin', _markRowHtml(bk));
    }).catch(() => {});
  }

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
        a.className = 'btn btn-ghost komga-read-link';
        a.href = `${komgaBase()}/book/${fi.komga_book_id}/read`;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'Komga';
        footer.insertBefore(a, footer.querySelector('.btn-primary'));
      }
    }).catch(() => {});
  }

  // Fetch issue details async from LOCG (keyless). Credits arrive as flat
  // [{role, name}] ready for _renderIssueDetails.
  if (hasLocgId) {
    try {
      const d = await api.get(`/api/series/${seriesId}/issues/${number}/locg-details`);
      _renderIssueDetails(d.desc, d.credits || []);
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
      <div class="v-card" id="vc-${esc(c.id)}" style="animation-delay:${i*STAGGER_MS}ms" onclick="_imOpenLightbox('${esc(c.id)}')">
        <div class="v-cover">
          <img src="${esc(c.thumb)}" alt="${esc(c.name)}" loading="lazy"
            onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
          <div class="no-img" style="display:none">No image</div>
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
  if (n === 0) { hint.innerHTML = 'Click a cover to view it large — include or ★ it from there.'; return; }
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
  _imApplyVariants(_issueVariantSeriesId, _issueVariantNumber, _issueVariantOwned);
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
    // Only mutate the cache when we're still ON this series — _detailSeries
    // lingers after navigation and #N matches the wrong series' issue otherwise.
    const onView = currentView === 'series-detail' && currentParams.id === seriesId;
    const obj = onView ? _detailSeries?.issues?.find(i => i.number === number) : null;
    if (obj) obj.variant_cover = newSrc;
    if (obj && newSrc) _crossfadeTileCover(number, newSrc);
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

async function setIssueIgnored(seriesId, number, ignored) {
  try {
    await api.patch(`/api/series/${seriesId}/issues/${number}/ignore`, { ignored });
  } catch (e) {
    showToast('Couldn’t update issue'); console.error(e); return;
  }
  closeModal();
  showToast(ignored ? `#${fmtNum(number)} ignored — no more searching for it`
                    : `#${fmtNum(number)} back on the list`);
  renderSeriesDetail(seriesId);
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
const _PTR_VIEWS = new Set(['library', 'ondeck', 'needs-match', 'series-detail', 'pull-list', 'activity']);
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
    _autoPollDone.delete(id);     // you asked for fresh: earn a fresh auto-populate poll
    syncSeries(id, null, null, true);   // you pulled = you asked: full LOCG refresh
    showToast('Syncing series…');
    return renderSeriesDetail(id);
  }
  if (currentView === 'pull-list')     return _renderPullListContent();
  if (currentView === 'needs-match')   return renderNeedsMatch();
  if (currentView === 'ondeck')        return renderOnDeck();
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

// iPadOS tells Safari it can hover, so '(hover: none)' never matches there and
// every hover-revealed button stayed hidden. Coarse pointer or a touch API is
// the honest signal: on touch, the buttons are simply there.
if ((window.matchMedia && window.matchMedia('(pointer: coarse)').matches) || 'ontouchstart' in window || navigator.maxTouchPoints > 0) {
  document.body.classList.add('touch');
}

async function boot() {
  // Always land on the library. Komga is an optional integration, configured
  // in Settings — never a blocking welcome gate. Kometa runs fine without it:
  // search and track via LOCG, own via folders.
  const cfg = await api.get('/api/config');
  _appConfig = cfg;
  const { view, params } = _parseHash();
  navigate(view, params);
  _startBadgePolling();
  // the Needs matching item's count, without waiting for the Library to load
  api.get('/api/series').then(rows => _updateNeedsBadge(rows.filter(_needsMatch).length)).catch(() => {});
}

boot();


// --- Favourites + ratings (kometa/marks.py) --------------------------------------
// A star on anything, 1–5 on an issue. Reading state: keyed by row id, not path.
function _favBtn(s) {
  const r = s.rating || s.rating_derived;
  return `<button class="btn btn-sm btn-ghost fav-btn${s.favourite ? ' on' : ''}" id="fav-btn-${s.id}" title="${s.favourite ? 'Favourite — tap to remove' : 'Favourite'}"
    onclick="_toggleSeriesFav(${s.id}, this)">${s.favourite ? '★' : '☆'}${r ? ` <span class="fav-rating">${r}</span>` : ''}</button>`;
}

async function _toggleSeriesFav(id, btn) {
  const on = !btn.classList.contains('on');
  btn.classList.toggle('on', on); btn.firstChild.textContent = on ? '★' : '☆';
  try { await api.put(`/api/series/${id}/mark`, { favourite: on }); if (_detailSeries?.id === id) _detailSeries.favourite = on; }
  catch { btn.classList.toggle('on', !on); btn.firstChild.textContent = on ? '☆' : '★'; showToast('Couldn’t save that', 'error'); }
}

async function _toggleBookFav(bookId) {
  try {
    const cur = await api.get(`/api/books/${bookId}`);
    const r = await api.put(`/api/books/${bookId}/mark`, { favourite: !cur.favourite });
    showToast(r.favourite ? 'Favourited' : 'Removed from favourites');
  } catch { showToast('Couldn’t save that', 'error'); }
}

function _markRowHtml(bk) {
  const dots = [1, 2, 3, 4, 5].map(n => `<button class="rating-dot${bk.rating >= n ? ' on' : ''}" data-n="${n}" title="${n} of 5" aria-label="Rate ${n}"
      onclick="_setBookMark(${bk.id}, {rating: ${n}}, this.closest('.mark-row'))">★</button>`).join('');
  return `<div class="mark-row" id="mark-row-${bk.id}">
    <button class="mark-star${bk.favourite ? ' on' : ''}" title="Favourite" aria-label="Favourite"
      onclick="_setBookMark(${bk.id}, {favourite: !this.classList.contains('on')}, this.closest('.mark-row'))">${bk.favourite ? '★' : '☆'}</button>
    <span class="rating-dots">${dots}</span>
    ${bk.rating ? `<button class="rating-clear u-label" onclick="_setBookMark(${bk.id}, {clear_rating: true}, this.closest('.mark-row'))">clear</button>` : ''}
  </div>`;
}

async function _setBookMark(bookId, patch, row) {
  try {
    const r = await api.put(`/api/books/${bookId}/mark`, patch);
    if (row) row.outerHTML = _markRowHtml({ id: bookId, ...r });
  } catch { showToast('Couldn’t save that', 'error'); }
}
