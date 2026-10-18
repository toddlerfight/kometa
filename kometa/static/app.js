// --- API ---

let _appConfig = {};
let _issueModalPollTimer = null;

// Per-item stagger for cascading grid/list entrances (ms). One value so cascades
// feel consistent everywhere. Pairs with the CSS motion tokens in style.css :root.
const STAGGER_MS = 40;

// Out in the last 7 days, today included (local dates, 'YYYY-MM-DD…').
function _thisWeek(d) {
  if (!d) return false;
  const iso = t => { const x = new Date(t); return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`; };
  const day = String(d).slice(0, 10), now = Date.now();
  return day <= iso(now) && day >= iso(now - 6 * 86400000);
}

// Signed out (accounts on, the session gone): back to / — which is the sign-in page now.
function _signedOut(r) { if (r.status === 401) location.replace('/'); }

const api = {
  get(url) {
    return fetch(url).then(r => { _signedOut(r); if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { _signedOut(r); if (!r.ok) throw new Error(r.status); return r.status === 204 ? null : r.json(); });
  },
  patch(url, body) {
    return fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { _signedOut(r); if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  put(url, body) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => { _signedOut(r); if (!r.ok) throw new Error(r.status); return r.json(); });
  },
  del(url) {
    return fetch(url, { method: 'DELETE' }).then(r => {
      _signedOut(r); if (!r.ok) throw new Error(r.status);
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
let detailSortRated = false;   // ★: the community's order, best first (kometa/toprated.py)
let _topRated = null;          // {id, by: {number: score}} — the run on screen, owned + rated only
let _autoTabFor = null;   // series id we've already made the B1 trades-default call for
let _wantTab = null;      // a tab asked for by whoever navigated here (a trade's 'Go to series' → Trades)

function navigate(view, params = {}) {
  // going somewhere closes whatever window is open (a series card in the creator window opened
  // the series BEHIND the window, 2026-10-17) — in three beats, see _vtOverModal
  if (!document.getElementById('modal')?.classList.contains('hidden')) _vtOverModal();
  _navBegin(view, params, 'fwd');
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
    history.replaceState({ view, params, idx: _histIdx }, '', hash);
  } else {
    history.pushState({ view, params, idx: ++_histIdx }, '', hash);
  }
  updateNav();
  renderView();
}

window.addEventListener('popstate', e => {
  const { view, params } = _parseHash();
  // Back or forward? Every entry we push carries its depth — a shallower one is Back.
  const idx = e.state?.idx ?? 0;
  _navBegin(view, params, idx < _histIdx ? 'back' : 'fwd');
  _histIdx = idx;
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

// --- View transitions + the loading bar ---------------------------------------
// The app got so fast that views started SLAMMING in like a door in a gale.
// This is the motion between them: the old view fades off a ghost while the new
// one renders underneath, unseen, then settles in once it has actually painted.
// The renderers never know — they write #app exactly as before, ids and all.
//
// The ghost: #app's old children MOVED (not cloned — covers stay decoded, no
// re-download, no second blur-in) into a CLOSED shadow root wearing a copy of the
// house stylesheet. Closed = invisible to getElementById/querySelector, so a
// renderer asking "is #browse-search already here?" can't be fooled by a corpse.

const _motionOK = () => !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
// A poller that keeps going from a backgrounded tab is a request the server answers
// for nobody. Returns true when the caller should just reschedule and do nothing.
const _tabHidden = () => document.hidden === true;

let _histIdx = (history.state && history.state.idx) || 0;
const _vt = { gen: 0, ghost: null, t0: 0, dir: 'fwd', inTimer: null, dogTimer: null, holdUntil: 0 };

// A motion token from style.css, in ms ('.15s' → 150) — the house timings, read not retyped.
const _cssMs = name => (parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0) * 1000;

// Leaving a page from inside a window, in the house's own moves, one after another: the page
// behind goes dark (the ghost's vt-out, --t-fast), then the window sinks out (modal-out,
// --t-mid), then the new page rises (vt-settle) — held back until the window is gone.
// A window already on its way out just finishes; reduced motion closes it at once.
function _vtOverModal() {
  const closing = document.getElementById('modal-backdrop')?.classList.contains('closing');
  if (closing || !_motionOK()) { closeModal(); if (closing) _vt.holdUntil = performance.now() + _cssMs('--t-mid'); return; }
  const dark = _cssMs('--t-fast');
  _vt.holdUntil = performance.now() + dark + _cssMs('--t-mid');
  setTimeout(closeModal, dark);
}
const _navBar = { timer: null, on: false };

// navigate/popstate call this BEFORE current{View,Params} move, and it decides
// whether the hop earns a transition at all. The reader is its own layer (it
// fades the whole layout — reader.js), and re-navigating to the thing you're
// already on (same view, same id) is a quiet repaint, not a journey.
function _navBegin(view, params, dir) {
  const gen = ++_vt.gen;
  if (view === 'read') {                                 // the reader brings its own loader
    _navBarDone();
    document.getElementById('content')?.classList.remove('vt-pending');   // never strand the view under it invisible
    return;
  }
  _navBarStart();
  const fromView = currentView, fromParams = currentParams || {};
  if (fromView === 'read') {                             // closing the reader: it fades off the view underneath
    _vt.quiet = { gen, y: document.getElementById('app')?.scrollTop || 0 };
    return;
  }
  const sameThing = fromView === view && (fromParams.id ?? fromParams.key) === (params?.id ?? params?.key);
  if (sameThing || !_motionOK()) return;                 // reduced motion = an honest instant swap
  _vtLeave(dir, gen);
}

function _vtLeave(dir, gen) {
  const content = document.getElementById('content'), app = document.getElementById('app');
  if (!content || !app) return;
  clearTimeout(_vt.inTimer);
  content.classList.remove('vt-in', 'vt-in-back');
  _vt.dir = dir;
  _vt.t0 = performance.now();
  // Hopping again mid-leave: the ghost already on screen IS the view you left —
  // let it keep fading; whatever half-rendered thing is in #app just gets overwritten.
  if (!_vt.ghost && app.childNodes.length) _vt.ghost = _vtGhost(content, app);
  content.classList.add('vt-pending');
  // A view that never settles (a hung fetch) still gets shown — its own
  // "Loading..." beats a black hole. The bar keeps crawling regardless.
  clearTimeout(_vt.dogTimer);
  _vt.dogTimer = setTimeout(() => _vtReveal(gen), 1500);
}

let _vtSheets = null;
function _vtStyleSheets() {
  if (_vtSheets) return _vtSheets;
  if (typeof CSSStyleSheet !== 'function' || !('adoptedStyleSheets' in Document.prototype)) return (_vtSheets = []);
  try {
    const css = [];
    for (const sh of document.styleSheets) {
      // the font CSS is cross-origin (unreadable) — and @font-face crosses shadow lines anyway
      if (sh.href && !sh.href.includes('/style.css')) continue;
      for (const r of sh.cssRules) css.push(r.cssText);
    }
    const house = new CSSStyleSheet();
    house.replaceSync(css.join('\n'));
    // A frozen frame: no cover re-blur, no card re-cascade as the nodes land in the
    // ghost. Every animation jumps straight to its END state — not `animation: none`,
    // which strands the opacity:0-until-it-plays cards (.card-cascade) invisible.
    const still = new CSSStyleSheet();
    still.replaceSync(`.vt-ghost-wrap { position: absolute; inset: 0; display: flex; flex-direction: column; overflow: hidden; }
      *, *::before, *::after { animation-duration: 1ms !important; animation-delay: -1s !important;
        animation-iteration-count: 1 !important; transition: none !important; }`);
    _vtSheets = [house, still];
  } catch { _vtSheets = []; }
  return _vtSheets;
}

function _vtGhost(content, app) {
  const sheets = _vtStyleSheets();
  if (!sheets.length) return null;                     // no constructable sheets: the old view just goes, the new one still fades in
  const host = document.createElement('div');
  host.className = 'vt-ghost';
  host.setAttribute('aria-hidden', 'true');
  host.inert = true;
  const root = host.attachShadow({ mode: 'closed' });
  root.adoptedStyleSheets = sheets;
  const wrap = document.createElement('div');
  wrap.className = 'vt-ghost-wrap';
  const box = document.createElement('main');
  box.id = 'app';                                      // shadow-scoped: the house #app rules apply, the document never sees it
  box.className = app.className;
  const scroll = app.scrollTop;
  box.append(...app.childNodes);
  wrap.append(document.getElementById('series-bg').cloneNode(true), document.getElementById('topbar').cloneNode(true), box);
  root.append(wrap);
  content.appendChild(host);
  box.scrollTop = scroll;
  const gone = () => { host.remove(); if (_vt.ghost === host) _vt.ghost = null; };
  host.addEventListener('animationend', gone, { once: true });
  setTimeout(gone, 400);                               // a backgrounded tab never fires animationend
  return host;
}

// The renderer settled (painted, or failed into its Retry). The bar finishes;
// the new view settles in — no earlier than most of the way through the ghost's
// fade, so the two overlap a hair instead of stacking end to end.
function _navSettled(gen) {
  if (gen !== _vt.gen) return;                         // a slow loser from a view you've already left
  _navBarDone();
  const wait = Math.max(0, _vt.t0 + 110 - performance.now(), _vt.holdUntil - performance.now());
  setTimeout(() => requestAnimationFrame(() => _vtReveal(gen)), wait);
}

function _vtReveal(gen) {
  if (gen !== _vt.gen) return;
  clearTimeout(_vt.dogTimer);
  const content = document.getElementById('content');
  if (!content || !content.classList.contains('vt-pending')) return;
  content.classList.remove('vt-pending');
  _vt.holdUntil = 0;
  content.classList.add(_vt.dir === 'back' ? 'vt-in-back' : 'vt-in');
  // a timer, not animationend: every cascading card underneath bubbles its own
  clearTimeout(_vt.inTimer);
  _vt.inTimer = setTimeout(() => content.classList.remove('vt-in', 'vt-in-back'), 340);
}

// The bar: only for a view that's genuinely slow. Under ~100 ms it never shows.
function _navBarStart() {
  const bar = document.getElementById('nav-bar');
  if (!bar) return;
  clearTimeout(_navBar.timer);
  if (_navBar.on) return;                              // still crawling for the hop before: keep going
  _navBar.timer = setTimeout(() => {
    const fill = bar.firstElementChild;
    fill.style.transition = 'none';
    fill.style.transform = 'scaleX(0)';
    void fill.offsetWidth;
    fill.style.transition = '';                        // the CSS crawl: quick off the line, never arrives on its own
    fill.style.transform = 'scaleX(.9)';
    bar.classList.add('on');
    _navBar.on = true;
  }, 100);
}

function _navBarDone() {
  clearTimeout(_navBar.timer);
  if (!_navBar.on) return;
  _navBar.on = false;
  const bar = document.getElementById('nav-bar');
  if (!bar) return;
  const fill = bar.firstElementChild;
  fill.style.transition = 'transform var(--t-mid) var(--ease-out)';
  fill.style.transform = 'scaleX(1)';
  bar.classList.remove('on');                          // fades a beat later (CSS transition-delay), once the fill lands
}

function updateNav() {
  const navView = (currentView === 'series-detail' || currentView === 'shelf' || currentView === 'stack') ? 'library'
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
  const app = document.getElementById('app');
  // Back from the reader: the view underneath is still painted. Its refresh keeps
  // it there through 'Loading...' and lands at the same scroll — no flash to an
  // empty page and a lurch back to the top while the reader is still fading off.
  const q = _vt.quiet;
  if (q && q.gen === _vt.gen) {
    if (/^<div class="state-msg">Loading/.test(html)) return;
    _vt.quiet = null;
    app.innerHTML = html;
    app.scrollTop = q.y;
    requestAnimationFrame(() => { app.scrollTop = q.y; });   // rows that paint a beat later
    return;
  }
  app.innerHTML = html;
}

function renderView() {
  const view = currentView;
  const gen = _vt.gen;
  // The reader is a full-screen layer over whatever view you came from — the
  // view underneath stays painted, so Back is instant.
  if (view === 'read') return renderReader(currentParams);
  // Back from the reader: refresh the view BEHIND it while the reader still covers
  // the screen, and only then fade — one clean crossfade onto a finished page, not
  // a fade onto a page still rebuilding itself.
  const fromReader = !!(_vt.quiet && _vt.quiet.gen === gen);
  if (!fromReader) closeReader();
  const paint = (() => {
    switch (view) {
      case 'library':       return renderLibraryBrowse();
      case 'stack':         return renderStackPage(currentParams.key);
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
  }).then(() => fromReader && _underReady()).finally(() => {
    if (fromReader) closeReader();
    _navSettled(gen);
  });
}

// The refreshed view is in the DOM; give the covers on screen a moment to decode so
// the fade lands on pictures, not grey boxes. Capped — a slow cover never holds you.
function _underReady(capMs = 450) {
  const app = document.getElementById('app');
  const h = window.innerHeight;
  const imgs = [...(app?.querySelectorAll('img') || [])].filter(i => {
    const r = i.getBoundingClientRect();
    return r.bottom > 0 && r.top < h;
  }).slice(0, 24);
  const decoded = Promise.all(imgs.map(i => i.decode ? i.decode().catch(() => {}) : Promise.resolve()));
  return Promise.race([decoded, new Promise(r => setTimeout(r, capMs))])
    .then(() => new Promise(r => requestAnimationFrame(() => r())));
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

// Kometa's own cover route for an issue: your chosen variant, else the file's own
// first page (kept in the image store), else the catalogue's.
function bookThumb(issue) {
  return `/api/series/${issue.tracked_series_id}/issues/${issue.number}/thumbnail?c=2`;   // c: bumped when covers got bigger
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
  // no date = a back issue the catalogue only has a cover date for (Batgirl: Year One, 2003): it's
  // out, so it's missing — amber, a ↓, on the Missing tab — the way the server already counts it.
  // Unscheduled future issues carry 9999-01-01, not nothing, and stay upcoming.
  if (!issue.store_date) return 'missing';
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
      const s = await api.get(`/api/series/${id}/status`);
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
            || s.issue_count !== (before.issue_count ?? (before.issues || []).length);
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

// The Library's data — /api/series plus the shelf — fetched once and shared by
// every view (Library, stacks, Needs matching, the boot badge, File under):
// boot used to fetch the 719 KB list twice at once, and every visit refetched it.
const _lib = { series: null, shelf: null, at: 0, inflight: null };

function _libFetch() {
  if (_lib.inflight) return _lib.inflight;
  _lib.inflight = Promise.all([
    api.get('/api/series'),
    api.get('/api/shelf').catch(() => ({ series: [], tracked_reading: {} })),
  ]).then(([series, shelf]) => {
    Object.assign(_lib, { series, shelf, at: Date.now(), inflight: null });
    _updateNeedsBadge(series.filter(_needsMatch).length);
    return _lib;
  }).catch(e => { _lib.inflight = null; throw e; });
  return _lib.inflight;
}

// the tracked list, reusing one that's a few seconds old
async function seriesRows(maxAgeMs = 15000) {
  if (_lib.series && Date.now() - _lib.at < maxAgeMs) return _lib.series;
  return (await _libFetch()).series;
}

// what a repaint would change — so a background refresh that brings nothing new doesn't rebuild the grid
function _libSig() {
  const r = _lib.series || [], sh = (_lib.shelf && _lib.shelf.series) || [];
  return r.map(s => `${s.id}:${s.owned}:${s.missing}:${s.upcoming}:${s.calendar_date}:${s.favourite}:${s.rating}:${s.match_status}:${s.title}`).join('|')
    + '#' + sh.map(s => `${s.id}:${s.count || s.book_count || ''}`).join('|') + '#' + JSON.stringify(_lib.shelf?.tracked_reading || {}).length;
}

function _paintLibrary(quiet = false) {
  // The library is the whole shelf: tracked series (acquisition) + every other
  // series folder (reader-only). Shelf failing must not cost the tracked grid.
  const tracked = _lib.series || [], shelf = _lib.shelf || { series: [], tracked_reading: {} };
  const tr = shelf.tracked_reading || {};
  browseState._trackedCount = tracked.length;
  // Unmatched series live in their own section (Needs matching) until you've
  // picked the run — the Library is the matched shelf.
  browseState._cache = tracked.filter(s => !_needsMatch(s)).map(s => ({ ...s, kind: 'series', ...(tr[s.id] || {}) }))
    .concat((shelf.series || []).map(s => ({ ...s, kind: 'shelf' })));
  _renderBrowseResults(quiet);
}

let browseState = { search: '', searchTimer: null, toggles: { upcoming: false, missing: false, pulling: false, stacks: true }, _cache: null, sortKey: 'date', sortDir: { date: 'asc' }, stack: null };

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
  browseState._animate = true;          // a fresh visit: the first cards cascade in
  browseState.toggles = { upcoming: false, missing: false, pulling: false, stacks: true };
  browseState._cache  = null;
  browseState.stack   = null;
  // The sort you chose last time sticks (per browser); nearest release first by default.
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('kometa.librarySort') || 'null'); } catch {}
  browseState.sortKey = saved?.key || 'date';
  browseState.sortDir = saved?.dir || { date: 'asc' };
  setApp('<div class="state-msg">Loading...</div>');
  await _loadBrowsePage();
  // arrived from the header's search: run it here, people and all
  if (browseState.search) {
    const lib = document.getElementById('browse-search');
    if (lib) { lib.value = browseState.search; lib.focus(); }
    browseSearch(browseState.search);
  }
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
  { key: 'favourites', label: ico('heart') + ' Favourites' },
  { key: 'rated',      label: ico('star') + ' Rated' },      // anything you've starred, its own rating or its issues'
  { key: 'runs',       label: 'Runs' },         // hide the one-shots and single-issue series
  { key: 'specials',   label: 'Specials' },     // unfold annuals / one-shots from under their runs
  { key: 'stacks',     label: 'Stacks' },       // franchises as one card (Aliens · 45 series); off = flat
];

const _isReading = s => (s.in_progress ?? 0) > 0 || (s.read_count ?? 0) > 0;

function _browseFilterTabs() {
  return `<div class="browse-filters">
    ${BROWSE_TOGGLES.filter(f => !(browseState.stack && f.key === 'stacks')).map(f => `
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
      <button class="sort-btn${browseState.sortKey === 'read' ? ' active' : ''}" id="sort-read"
        onclick="browseSort('read')" title="Sort by last read">
        <span class="sort-btn-icon">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="7" cy="7" r="5.5"/><polyline points="7,3.5 7,7 9.5,8.5"/>
          </svg>
        </span>
      </button>
      <button class="sort-btn${browseState.sortKey === 'added' ? ' active' : ''}" id="sort-added"
        onclick="browseSort('added')" title="Sort by newest file added">
        <span class="sort-btn-icon">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M2 10.5V3.5a1 1 0 0 1 1-1h3l1.5 1.5H11a1 1 0 0 1 1 1v5.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z"/><line x1="7" y1="6" x2="7" y2="10"/><line x1="5" y1="8" x2="9" y2="8"/>
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
    // newest first is the useful default for the two recency sorts
    browseState.sortDir[key] = browseState.sortDir[key] || ((key === 'read' || key === 'added') ? 'desc' : 'asc');
  }
  try { localStorage.setItem('kometa.librarySort', JSON.stringify({ key: browseState.sortKey, dir: browseState.sortDir })); } catch {}
  const arrowBox = document.getElementById('sort-arrow');
  if (arrowBox) {
    arrowBox.textContent = browseState.sortDir[browseState.sortKey] === 'asc' ? '↑' : '↓';
    arrowBox.classList.toggle('non-default', !_isDefaultSort());
  }
  for (const k of ['alpha', 'date', 'read', 'added']) document.getElementById(`sort-${k}`)?.classList.toggle('active', browseState.sortKey === k);
  _renderBrowseResults();
}

// A stack's page: the same Library, narrowed to one franchise's members.
async function renderStackPage(key) {
  setTopbar();
  document.getElementById('topbar-actions').innerHTML = `<button class="btn btn-ghost btn-sm" onclick="navigate('library')">‹ Library</button>`;
  browseState.search  = '';
  browseState._animate = true;          // a fresh visit: the first cards cascade in
  browseState.toggles = { upcoming: false, missing: false, pulling: false, stacks: true };
  browseState._cache  = null;
  browseState.stack   = key;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('kometa.librarySort') || 'null'); } catch {}
  browseState.sortKey = saved?.key || 'alpha';
  browseState.sortDir = saved?.dir || { alpha: 'asc' };
  setApp('<div class="state-msg">Loading...</div>');
  await _loadBrowsePage();
  const any = (browseState._cache || []).find(s => s.franchise?.key === key);
  document.getElementById('topbar-title').textContent = any ? any.franchise.name : 'Stack';
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

  // Paint from what we already have, then check with the server: Back to the
  // Library re-downloaded and rebuilt everything before showing a card.
  const fresh = _libFetch();
  const had = _lib.series ? _libSig() : null;
  if (!had) await fresh;                     // nothing to show yet: a failure is the view's error + Retry
  _paintLibrary();
  if (had) fresh.then(() => {
    if (_libSig() === had || !['library', 'stack'].includes(currentView)) return;
    const box = document.getElementById('app'), top = box?.scrollTop;
    _paintLibrary(true);
    if (box) box.scrollTop = top;
  }).catch(() => {});
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

// The cascade plays on a fresh Library's first cards only. It was on every card
// (~900: everything past #15 started at once) and replayed on every filter tap
// and keystroke, each time recreating the covers' blur-in too.
function _casc(i) {
  return browseState._animate && i < 15 ? ` card-cascade" style="animation-delay:${i * STAGGER_MS}ms` : '';
}

function _renderBrowseResults(quiet = false) {
  const { toggles, search, sortKey, sortDir, _cache: all, stack } = browseState;
  if (!all) return;
  if (quiet) browseState._animate = false;

  const q = search.toLowerCase();
  const anyToggleOn = toggles.upcoming || toggles.missing;
  const ppl = browseState.people && browseState.people.q === search ? browseState.people : null;
  const why = ppl ? ppl.why : {};
  let filtered = all.filter(s => {
    if (stack && s.franchise?.key !== stack) return false;               // a stack's page: its members only
    if (q && !s.title.toLowerCase().includes(q) && !(s.kind !== 'shelf' && why[s.id])) return false;
    // Pull list = what you're pulling that has something still to come, like the
    // Pull list section — not every pulled run whether or not anything's out
    if (toggles.pulling && !(s.kind === 'series' && s.on_pull_list && (s.next_upcoming || _thisWeek(s.calendar_date)))) return false;
    if (toggles.favourites && !s.favourite) return false;
    if (toggles.rated && !s.rating) return false;
    // folded under its run — unless you searched, or asked for what you're pulling:
    // a filtered list never hides a match inside another card (US of Anger #4 sat
    // inside 100 Bullets while the Pull list said it was out next week)
    if (s.family_parent && !toggles.specials && !search && !toggles.pulling) return false;
    // Neither toggle on -> no narrowing (the default, everything). Either on ->
    // UNION: "needs attention" (upcoming release OR missing issue), not the
    // (much rarer, and less useful) intersection of both at once.
    if (!anyToggleOn) return true;
    // Missing = gaps you've ASKED for. A run you own 12/40 of and aren't
    // pulling isn't missing anything — it's just a partial run.
    return (toggles.upcoming && (s.upcoming ?? 0) > 0)
        || (toggles.missing && s.on_pull_list && (s.missing ?? 0) > 0);
  });

  // Stacks: a franchise (five or more top-level series sharing a leading name,
  // stamped by kometa/family.py) is ONE card here and a page of its own. A
  // search looks inside stacks and shows the member, not the stack.
  let items = filtered;
  if (toggles.stacks && !search && !stack && !toggles.pulling) {          // pulling: every series flat, no stacks
    const groups = new Map();
    items = [];
    for (const s of filtered) {
      const f = s.franchise;
      if (!f) { items.push(s); continue; }
      let g = groups.get(f.key);
      if (!g) { g = { kind: 'stack', key: f.key, title: f.name, count: f.count, members: [] }; groups.set(f.key, g); items.push(g); }
      g.members.push(s);
    }
  }
  // Runs: hide the one-shots and single-issue series; a stack keeps its members
  if (toggles.runs) items = items.filter(s => s.kind === 'stack' || !_isSingle(s));

  // the date a card shows and sorts by: the calendar (this week's release, else the
  // next) — except on the Pull list, where it's the NEXT issue still to come: a
  // series that came out last Wednesday and has #25 next month is next month's
  // ...unless it came out THIS week: the issue that just landed is the one you go
  // looking for, so it stays this week's until next Wednesday (2026-10-14)
  const dateOf = s => toggles.pulling ? (_thisWeek(s.calendar_date) ? s.calendar_date : s.next_upcoming) : s.calendar_date;
  browseState._dateOf = dateOf;
  const latest = (list, f) => list.reduce((m, x) => { const v = f(x); return v && (!m || v > m) ? v : m; }, null);
  const val = {
    alpha: s => _sortTitle(s.title),
    date:  s => s.kind === 'stack' ? _stackDate(s, dateOf) : dateOf(s),
    read:  s => s.kind === 'stack' ? latest(s.members, m => m.last_read) : s.last_read,
    added: s => s.kind === 'stack' ? latest(s.members, m => m.newest_file_at) : s.newest_file_at,
  }[sortKey] || (() => null);
  const dir = (sortDir[sortKey] ?? 'asc') === 'asc' ? 1 : -1;
  // same date (or none): by title, so 'Namwolf sits with the Ns and not before 100 Bullets
  const byTitle = (a, b) => _sortTitle(a.title).localeCompare(_sortTitle(b.title), 'en', { sensitivity: 'base', numeric: true });
  items = items.slice().sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va == null && vb == null) return byTitle(a, b);
    if (va == null) return 1;                               // nothing to sort on sinks, whatever the direction
    if (vb == null) return -1;
    return dir * String(va).localeCompare(String(vb)) || byTitle(a, b);
  });

  const peopleRow = ppl && ppl.people.length ? `<div class="people-row">${ppl.people.map(p => `
      <button class="person-chip" onclick="_personPick(${jsArg({ id: p.id, name: p.name })})"
        title="${p.count} series on your shelf">
        <span class="person-name">${esc(p.name)}</span>
        <span class="person-roles">${esc(p.roles.slice(0, 3).join(', '))} · ${p.count}</span></button>`).join('')}</div>` : '';

  if (!items.length) {
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
    document.getElementById('browse-results').innerHTML = peopleRow + empty;
    _paintLetterRail([]);
    return;
  }

  const letters = sortKey === 'alpha';
  let lastLetter = null;
  const seen = [];
  const cards = items.map((s, i) => {
    let head = '';
    if (letters) {
      const L = _letterOf(s.title);
      if (L !== lastLetter) { lastLetter = L; seen.push(L); head = `<div class="letter-head" id="letter-${L === '#' ? 'num' : L}">${L}</div>`; }
    }
    if (s.kind === 'stack') return head + _stackCardHtml(s, i);
    const pub   = s.publisher ? `<div class="series-card-publisher u-truncate">${esc(s.publisher.toUpperCase())}</div>` : '';
    if (s.kind === 'shelf') return head + _shelfCardHtml(s, i, pub);
    // Release-day issues not yet down count toward the total and turn it amber —
    // 145/146, not a '145/145 complete' while #146 is out today.
    const gap   = (s.missing ?? 0) + (s.out_today ?? 0);
    const total = (s.owned ?? 0) + gap;
    const pct   = total ? Math.round((s.owned / total) * 100) : 0;
    // Amber only when you're pulling it: a gap is "missing" because you asked for
    // the series. Not pulling = grey count, still honest about what you own.
    const color = gap > 0 ? (s.on_pull_list ? 'var(--amb)' : 'var(--tq)')
                          : (total > 0 ? 'var(--pri)' : 'var(--tq)');
    const shownDate = (browseState._dateOf || (x => x.calendar_date))(s);
    const nextRelease = shownDate
      ? `<div class="series-card-next-release">${_fmtReleaseDate(shownDate)}</div>` : '';
    const thumbSrc  = s.card_image || `/api/series/${s.id}/thumbnail`;
    const thumbFall = s.card_image  ? `this.onerror=function(){this.onerror=null;this.style.opacity='0.15'};this.src='/api/series/${s.id}/thumbnail'` : `this.onerror=null;this.style.opacity='0.15'`;
    return head + `
      <div class="series-card${_casc(i)}" tabindex="0" role="button"
        onclick="navigate('series-detail', {id: ${s.id}})"
        onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.id}})">
        <div class="series-card-img-wrap">
          <img class="series-card-cover" src="${esc(thumbSrc)}" alt="${esc(s.title)}"
            loading="lazy" onerror="${thumbFall}">
          ${nextRelease}${s.favourite ? `<div class="series-card-fav" title="Favourite">${ico('heart')}</div>` : ''}
        </div>
        <div class="series-card-bar-track">
          <div class="series-card-bar-fill" style="width:${pct}%;background:${color}"></div>
        </div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(s.title)}</div>
          <div class="series-card-count" style="color:${color}">${s.owned}/${total}${s.family_children?.length ? ` <span class="series-card-specials" title="${s.family_children.length} special${s.family_children.length > 1 ? 's' : ''} folded under this run">+${s.family_children.length}</span>` : ''}</div>
        </div>
        ${pub}
        ${q && why[s.id] && !s.title.toLowerCase().includes(q) ? `<div class="series-card-publisher u-truncate rel-why">${esc(why[s.id])}</div>` : ''}
      </div>
    `;
  }).join('');

  document.getElementById('browse-results').innerHTML = peopleRow + `<div class="series-grid${letters ? ' with-letters' : ''}">${cards}</div>`;
  browseState._animate = false;              // filters, sorts and refreshes after this don't replay it
  _paintLetterRail(letters ? seen : []);
}

// A single issue filed as a series: a one-shot, an annual, an OGN, or a run the
// catalogue only knows one issue of. The Runs chip hides these.
function _isSingle(s) {
  if (s.kind === 'shelf') return (s.book_count ?? 0) <= 1;
  const n = (s.owned ?? 0) + (s.missing ?? 0) + (s.upcoming ?? 0);
  const t = (s.metron_type || '').toLowerCase();
  return n <= 1 && (!t || ['one-shot', 'one shot', 'annual', 'graphic novel'].includes(t));
}

// The title as it sorts and files: no leading 'The', no leading year or
// bracket, no leading dash — so 'The Walking Dead' sits under W.
function _sortTitle(t) {
  return (t || '').replace(/^[\s'"‘’“”.…¡¿*]+/, '').replace(/^\s*[-–—]+\s*/, '').replace(/^\s*[\[(](?:19|20)\d{2}[\])]\s*/, '').replace(/^(the|a|an)\s+/i, '').trim();
}
function _letterOf(t) {
  const c = _sortTitle(t).charAt(0).toUpperCase();
  return /[A-Z]/.test(c) ? c : '#';
}

// A stack's date: its soonest member's, by the same rule a single card's date follows.
function _stackDate(g, dateOf) {
  return g.members.map(dateOf || browseState._dateOf || (m => m.calendar_date)).filter(Boolean).sort()[0] || null;
}

// A franchise as one card: the first three members' covers, fanned.
function _stackCardHtml(g, i) {
  const covers = g.members.slice(0, 3).map(m => m.kind === 'shelf' ? `/api/shelf/${m.id}/cover` : (m.card_image || `/api/series/${m.id}/thumbnail`));
  const go = `navigate('stack', {key: ${JSON.stringify(g.key)}})`;
  const owned = g.members.reduce((n, m) => n + (m.owned ?? m.book_count ?? 0), 0);
  return `
      <div class="series-card stack-card${_casc(i)}" tabindex="0" role="button"
        data-stack="${esc(g.key)}" onclick="${esc(go)}" onkeydown="if(event.key==='Enter'||event.key===' ')${esc(go)}">
        <div class="series-card-img-wrap stack-fan">
          ${covers.map((u, k) => `<img class="series-card-cover stack-cover stack-cover-${k}" src="${esc(u)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">`).reverse().join('')}
          <div class="stack-badge">${g.count}</div>${(d => d ? `<div class="series-card-next-release">${_fmtReleaseDate(d)}</div>` : '')(_stackDate(g))}
        </div>
        <div class="series-card-bar-track"><div class="series-card-bar-fill" style="width:100%;background:var(--su3)"></div></div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(g.title)}</div>
        </div>
        <div class="series-card-publisher u-truncate">${g.count} series · ${owned} issues</div>
      </div>`;
}

// The A–Z jump rail: a fixed strip of the letters present; tap one, the grid
// scrolls to that letter's header. Painted only for an alphabetical sort.
function _paintLetterRail(letters) {
  document.getElementById('letter-rail')?.remove();
  if (!letters || letters.length < 2) return;
  const rail = document.createElement('div');
  rail.id = 'letter-rail';
  rail.className = 'letter-rail';
  rail.innerHTML = letters.map(L => `<button class="letter-rail-btn" onclick="document.getElementById('letter-${L === '#' ? 'num' : L}')?.scrollIntoView({block:'start', behavior:'smooth'})">${L}</button>`).join('');
  document.getElementById('browse-results')?.appendChild(rail);
}

// An untracked series: on the shelf, readable, nothing to fetch — so no release
// badge and no owned/missing bar. The count is books (or books read, once you've
// started), in the quiet colour.
function _shelfCardHtml(s, i, pub) {
  const go = `navigate('shelf', {id: ${s.id}})`;
  const count = _isReading(s) ? `${s.read_count}/${s.book_count} read` : `${s.book_count}`;
  const pct = _isReading(s) ? Math.round((s.read_count / s.book_count) * 100) : 0;
  return `
      <div class="series-card${_casc(i)}" tabindex="0" role="button"
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
  const bookCard = (c, tag, dismissable, tagHtml) => `
    <div class="issue-tile od-card" id="od-${c.book_id}" role="button" tabindex="0" title="${esc(c.series)} ${esc(c.label)}" onclick="${read(c)}" onkeydown="if(event.key==='Enter'||event.key===' ')${read(c)}">
      <div class="issue-tile-img"><img src="/api/books/${c.book_id}/cover" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        ${tagHtml || tag ? `<span class="od-tag">${tagHtml || esc(tag)}</span>` : ''}
        <button class="od-menu" title="Actions" aria-label="Actions" onclick="event.stopPropagation(); _bookActions(${jsArg({ book_id: c.book_id, series: c.series, label: c.label, number: c.number ?? null, series_id: c.series_id || null, shelf_id: c.shelf_id || null, dismissable: !!dismissable, completed: !!(c.progress && c.progress.completed), page_count: c.page_count || null })})">${DOTS}</button>
        ${dismissable ? `<button class="od-x" title="Mark as read" aria-label="Mark as read" onclick="event.stopPropagation(); _odMarkRead(${c.book_id}, ${c.progress ? c.progress.page : 1})">${ico('x')}</button>` : ''}
        ${c.progress ? `<div class="od-bar"><div style="width:${pct(c)}%"></div></div>` : ''}</div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>`;
  // an issue you don't have opens its card (2026-10-17), not the run
  const card = c => `showIssueModal(${c.series_id}, ${c.number}, {orSeries: true})`;
  const soonCard = c => `
    <div class="issue-tile od-card" role="button" tabindex="0" title="${esc(c.series)} ${esc(c.label)}" onclick="${card(c)}" onkeydown="if(event.key==='Enter'||event.key===' ')${card(c)}">
      <div class="issue-tile-img"><img src="/api/series/${c.series_id}/issues/${c.number}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
        ${c.status === 'upcoming' && c.store_date
          ? `<div class="series-card-next-release">${_fmtReleaseDate(c.store_date)}</div>`   // the Library's date pill, same everywhere
          : `<span class="od-tag${c.status === 'not_owned' ? ' amber' : ''}">${esc(c.status_label)}</span>`}</div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>`;
  const favCard = c => c.kind === 'series' ? `
    <div class="issue-tile od-card" role="button" tabindex="0" title="${esc(c.series)}" onclick="navigate('series-detail', {id: ${c.series_id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail', {id: ${c.series_id}})">
      <div class="issue-tile-img"><img src="/api/series/${c.series_id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"><span class="od-tag">${ico('heart')}</span></div>
      <div class="issue-tile-num">${esc(c.series)}</div>
    </div>` : bookCard(c, null, false, ico('heart'));
  // reading-list card: the books from where you are on, gaps as gaps — a gap in a
  // run you track opens the issue's card; any other opens the list, which is where
  // you deal with what's missing
  const gapGo = (c, l) => c.series_id && c.number != null && !isNaN(Number(c.number))
    ? `showIssueModal(${c.series_id}, ${Number(c.number)}, {orSeries: true})` : `navigate('readlist', {id: ${l.list_id}})`;
  const listCard = (c, l) => c.kind === 'gap' ? `
    <div class="issue-tile od-card od-gap" role="button" tabindex="0" title="${esc(c.label)} — not on the shelf" onclick="${gapGo(c, l)}" onkeydown="if(event.key==='Enter'||event.key===' ')${gapGo(c, l)}">
      <div class="issue-tile-img">${c.cover ? `<img src="${esc(c.cover)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="rl-nocover"></div>'}
        <span class="od-tag">${c.position}/${l.total}</span></div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>` : bookCard(c, c.tag || `${c.position}/${l.total}`);   // tag: an issue of a whole-run entry
  // Want to read: what you added, in order. A book card reads; one that isn't here
  // yet (an issue, or a run you've read all you own of) opens where to get it.
  const wantCard = c => c.kind === 'book' ? bookCard(c, c.want_series ? 'Next' : null)
    : c.kind === 'gap' ? `
    <div class="issue-tile od-card od-gap" role="button" tabindex="0" title="${esc(c.series)} ${esc(c.label)} — not on the shelf" onclick="${card(c)}" onkeydown="if(event.key==='Enter'||event.key===' ')${card(c)}">
      <div class="issue-tile-img"><img src="${esc(c.cover)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'"></div>
      <div class="issue-tile-num">${esc(c.label)}</div>
    </div>` : `
    <div class="issue-tile od-card od-gap" role="button" tabindex="0" title="${esc(c.series)} — nothing unread here" onclick="navigate('series-detail', {id: ${c.series_id}})" onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail', {id: ${c.series_id}})">
      <div class="issue-tile-img"><img src="/api/series/${c.series_id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"></div>
      <div class="issue-tile-num">${esc(c.series)}</div>
    </div>`;
  const lastRead = iso => {
    const d = new Date(iso);
    return 'Last read ' + d.toLocaleDateString(undefined, d.getFullYear() === new Date().getFullYear() ? { month: 'short' } : { month: 'short', year: 'numeric' });
  };
  const row = (title, help, cards, empty, cls = '') => `
    <div class="od-row${cls ? ' ' + cls : ''}">
      <div class="od-head"><span class="series-card-title">${title}</span></div>
      ${cards.length ? `<div class="issue-grid od-grid">${cards.join('')}</div>` : `<div class="od-empty">${empty}</div>`}
    </div>`;
  setApp(
    row('Continue reading', 'where you left off, most recent first', d.continue.map(c => bookCard(c, null, true)),
        'Nothing in progress. Open anything in the Library and it shows up here.') +
    ((d.want || []).length ? row('Want to read', '', d.want.map(wantCard), '') : '') +
    row('Next', 'the next unread issue in series you’ve finished something of', d.next.map(c => bookCard(c, 'Next')),
        'Finish an issue and its series lands here.') +
    row('Coming soon', 'series you’re caught up on whose next issue isn’t here yet', d.soon.map(soonCard),
        'Nothing waiting. Either you’re not caught up on anything, or everything you’re caught up on has nothing coming.') +
    row('Recently released', 'newest release dates you own, one card per series', (d.released || []).map(c => bookCard(c)),
        'Nothing with a release date yet.') +
    row('Recently added', 'newest files on the shelf, one card per series', (d.added || []).map(c => bookCard(c)),
        'Nothing new on the shelf.') +
    ((d.favourites || []).length ? row('Favourites', 'what you starred', d.favourites.map(favCard), '') : '') +
    (d.lists || []).map(l => row(`Next on ${esc(l.name)}`, '', l.cards.map(c => listCard(c, l)), '')).join('') +
    `<div class="od-slot" id="od-because"></div>` +
    `<div class="od-slot" id="od-readers"></div>` +
    // rows 7–10 sit behind More on a phone: the task rows and Because are the page
    ((d.rediscover || []).length ? row('Rediscover', '', d.rediscover.map(c => bookCard(c, lastRead(c.last_read_at))), '', 'od-extra') : '') +
    ((d.quick || []).length ? row('Quick reads', '', d.quick.map(c => bookCard(c, `${c.issues} issue${c.issues === 1 ? '' : 's'}`)), '', 'od-extra') : '') +
    `<div class="od-slot od-extra" id="od-picks"></div>` +
    `<div class="od-slot od-extra" id="od-likeyou"></div>` +
    `<div class="od-slot od-extra" id="od-trend"></div>` +
    `<button class="btn btn-ghost od-more" onclick="this.closest('#app').classList.add('od-all'); this.remove()">More</button>`
  );
  document.getElementById('app').classList.remove('od-all');
  // what the task rows already show never repeats in a discovery row
  const onPage = [...new Set([...d.continue, ...(d.want || []), ...d.next, ...d.soon, ...(d.released || []), ...(d.added || []),
    ...(d.lists || []).flatMap(l => l.cards), ...(d.rediscover || []), ...(d.quick || [])].map(c => c.series_id).filter(Boolean))];
  _loadBecause(onPage);
  _loadPicks();
  _loadReaders();
  _loadTrending();
  _watchOdRows();
}

// --- On Deck: the row bar -----------------------------------------------------------
// A strip of row names under the title (2026-10-17), in the Library's filter chips — the owner
// wants one nav look on every page and platform: tap one and the page scrolls to
// that row; the row you're on lights up as you scroll. Read off the rows on the page,
// so the ones that land later (Because, Readers, Trending, Picks) join as they arrive.
// Every 'Next on {list}' is one Lists stop, every 'Because…' one Because stop.
const _OD_STOPS = [[/^Continue reading$/, 'Continue'], [/^Want to read$/, 'Want'], [/^Next$/, 'Next'],
  [/^Coming soon$/, 'Coming'], [/^Recently released$/, 'Released'], [/^Recently added$/, 'Added'],
  [/^Favourites$/, 'Favourites'], [/^Next on /, 'Lists'], [/^Because you/, 'Because'], [/^Rediscover$/, 'Rediscover'],
  [/^Quick reads$/, 'Quick'], [/^Community picks$/, 'Picks'], [/^Readers like you$/, 'Readers'], [/^Trending/, 'Trending']];
let _odStops = [];
let _odRowWatch = null;

function _watchOdRows() {
  _odRowWatch?.disconnect();
  let t = null;
  _odRowWatch = new MutationObserver(() => {
    if (currentView !== 'ondeck') { _odRowWatch.disconnect(); return; }
    clearTimeout(t); t = setTimeout(_paintOdBar, 80);
  });
  _odRowWatch.observe(document.getElementById('app'), { childList: true, subtree: true });
  _paintOdBar();
}

function _paintOdBar() {
  if (currentView !== 'ondeck') return;
  const stops = [];
  for (const row of document.querySelectorAll('#app .od-row')) {
    const title = row.querySelector('.od-head .series-card-title')?.textContent.trim() || '';
    const label = (_OD_STOPS.find(([re]) => re.test(title)) || [null, title])[1];
    if (label && !stops.some(s => s.label === label)) stops.push({ label, row });
  }
  const sub = document.getElementById('topbar-sub');
  _odStops = stops;
  if (stops.length < 2) { sub.innerHTML = ''; return; }
  let bar = sub.querySelector('.od-rowbar');
  if (!bar) {
    sub.innerHTML = '<nav class="od-rowbar" aria-label="Jump to a row"></nav>';
    bar = sub.firstChild;
  }
  // in place, keyed by name: a row landing late adds its stop without replacing the
  // others (a redraw mid-tap lost the tap, 2026-10-17), and a stop jumps by name, so
  // one slotting in before it can't send it to the wrong row
  const have = new Map([...bar.children].map(b => [b.dataset.label, b]));
  stops.forEach((st, i) => {
    let b = have.get(st.label);
    if (!b) {
      b = document.createElement('button');
      b.className = 'browse-filter-tab u-label od-stop';      // the Library's chips: one nav look
      b.dataset.label = st.label;
      b.textContent = st.label;
      b.addEventListener('click', () => _odJump(st.label));
    }
    have.delete(st.label);
    if (bar.children[i] !== b) bar.insertBefore(b, bar.children[i] || null);
  });
  have.forEach(b => b.remove());
  _odMark();
}

function _odJump(label) {
  const s = _odStops.find(x => x.label === label);
  if (!s) return;
  const app = document.getElementById('app');
  if (!s.row.offsetParent) {                    // behind More on a phone: open it up first
    app.classList.add('od-all');
    app.querySelector('.od-more')?.remove();
  }
  const top = app.scrollTop + s.row.getBoundingClientRect().top - app.getBoundingClientRect().top - 12;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  app.scrollTo({ top: Math.max(0, top), behavior: reduce ? 'auto' : 'smooth' });
}

// the row you're on: the last one whose top has passed under the bar; at the very
// bottom, the last row (a short last row never reaches the top)
function _odMark() {
  if (currentView !== 'ondeck' || !_odStops.length) return;
  const app = document.getElementById('app');
  const line = app.getBoundingClientRect().top + 48;
  const shown = _odStops.map((s, i) => ({ s, i })).filter(x => x.s.row.offsetParent);
  let cur = shown.length ? shown[0].i : 0;
  for (const { s, i } of shown) if (s.row.getBoundingClientRect().top <= line) cur = i;
  if (shown.length && app.scrollTop + app.clientHeight >= app.scrollHeight - 4) cur = shown[shown.length - 1].i;
  const btns = document.querySelectorAll('#topbar-sub .od-stop');
  btns.forEach((b, i) => b.classList.toggle('active', i === cur));
  const bar = document.querySelector('#topbar-sub .od-rowbar'), on = btns[cur];
  if (bar && on) {                              // keep the lit stop in view in a strip wider than the screen
    const l = on.offsetLeft - bar.offsetLeft, r = l + on.offsetWidth;
    if (l < bar.scrollLeft || r > bar.scrollLeft + bar.clientWidth) bar.scrollLeft = Math.max(0, l - 24);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  let pending = false;
  document.getElementById('app')?.addEventListener('scroll', () => {
    if (pending || currentView !== 'ondeck') return;
    pending = true;
    requestAnimationFrame(() => { pending = false; _odMark(); });
  }, { passive: true });
});

// A catalogue card's cover (a series not on the shelf) goes through Kometa,
// which keeps it a day on disk — nine cards no longer load cover by cover.
function _ext(url) {
  return url && /^https?:\/\//.test(url) ? '/api/img?u=' + encodeURIComponent(url) : (url || '');
}

// A row that re-polls while the catalogue is still filling must not tear itself
// down and rebuild with the same content — that read as a flash, three times a
// page. Paint only when the payload actually changed.
function _rowUnchanged(selector, payload) {
  const key = JSON.stringify(payload);
  const el = document.querySelector(selector);
  if (el && el.dataset.sig === key) return true;
  return false;
}
function _rowSig(el, payload) { if (el) el.dataset.sig = JSON.stringify(payload); }

// --- Trending (kometa/trending.py): what shops sold most, from ICv2 ----------------
function _trendCard(e) {
  const owned = e.owned && e.series_id;
  // a single from a run you track is an issue: you have it, a tap reads and the ⋯ is
  // its card; you don't, a tap is its card (2026-10-17)
  const single = owned && e.number != null;
  const go = single ? (e.have_issue ? `openIssueReader(${e.series_id}, ${e.number})` : `showIssueModal(${e.series_id}, ${e.number}, {orSeries: true})`)
    : owned ? `navigate('series-detail', {id: ${e.series_id}})`
    : `showCatalogueModal(${jsArg({ title: e.metron_title || e.locg_title || e.series, publisher: e.publisher, year: e.year, cover: e.cover, metron_series_id: e.metron_series_id, locg_series_id: e.locg_series_id, rank: e.rank, why: [e.number != null ? `#${fmtNum(e.number)} charted` : 'charted'] })})`;
  const label = e.number != null ? `#${fmtNum(e.number)}` : (e.vol != null ? `Vol ${e.vol}` : '');
  const action = owned ? (single && e.have_issue ? _cardDots(e.series_id, e.number) : '')
    : e.metron_series_id
      ? `<button class="od-menu rel-get" title="Track this series and put it on the pull list" onclick="event.stopPropagation(); _relTrack(${jsArg({ metron_id: e.metron_series_id, title: e.metron_title || e.series, publisher_name: e.publisher || '', year_began: e.year || null, on_pull_list: true })}, this)">TRACK</button>`
    : e.locg_series_id      // Metron hasn't got it; LOCG has
      ? `<button class="od-menu rel-get" title="Track this series and put it on the pull list" onclick="event.stopPropagation(); _relTrack(${jsArg({ locg_id: e.locg_series_id, title: e.locg_title || e.series, publisher_name: e.publisher || '', year_began: e.year || null, on_pull_list: true })}, this)">TRACK</button>`
      : `<button class="od-menu rel-get" title="Find it in the catalogue" onclick="event.stopPropagation(); showAddWizard(${jsArg(e.series)})">FIND</button>`;
  return `<div class="series-card rel-card${owned ? '' : ' rel-gap'}" tabindex="0" role="button" onclick="${go}" title="${esc(e.title)} · ${esc(e.publisher || '')}">
    <div class="series-card-img-wrap">${e.cover ? `<img class="series-card-cover" src="${esc(_ext(e.cover))}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
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
  if (!d.comics.length && !d.graphic_novels.length) { app.querySelector('.trend-row')?.remove(); return; }
  if (d.pending && attempt < 4) setTimeout(() => _loadTrending(attempt + 1), 5000);
  const sig = { c: d.comics.slice(0, 24).map(e => [e.rank, e.cover, e.owned]), g: d.graphic_novels.slice(0, 12).map(e => [e.rank, e.cover, e.owned]) };
  if (_rowUnchanged('.trend-row', sig)) return;
  app.querySelector('.trend-row')?.remove();
  const month = d.month ? ` · ${esc(d.month)}` : '';
  (document.getElementById('od-trend') || app).insertAdjacentHTML('beforeend', `<div class="od-row trend-row">
    <div class="od-head"><span class="series-card-title">Trending</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">${month ? esc(d.month) + ' · ' : ''}ICv2</span></div>
    <div class="series-grid">${d.comics.slice(0, 24).map(_trendCard).join('')}</div>
    ${d.graphic_novels.length ? `<div class="od-head" style="margin-top:14px"><span class="series-card-title">Trending collected editions</span>
      <span class="u-label" style="color:var(--tq);margin-left:10px">${month ? esc(d.month) + ' · ' : ''}ICv2</span></div>
      <div class="series-grid">${d.graphic_novels.slice(0, 12).map(_trendCard).join('')}</div>` : ''}
  </div>`);
  _rowSig(app.querySelector('.trend-row'), sig);
}

// --- Community picks (kometa/toprated.py): your best-rated runs you haven't read
// to the end. The score where a run's count sits, how far in under the title. A part-read
// run shows here even when Continue has it too — that's the point of it.
function _pickCard(p) {
  const go = `navigate('series-detail', {id: ${p.series_id}})`;
  return `<div class="series-card rel-card" tabindex="0" role="button" onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}" title="${esc(p.title)}">
    <div class="series-card-img-wrap"><img class="series-card-cover" src="/api/series/${p.series_id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'"></div>
    <div class="series-card-footer"><div class="series-card-title">${esc(p.title)}</div>
      <div class="series-card-count comm-v${p.score >= 7.5 ? ' hi' : p.score < 6 ? ' lo' : ''}">${p.score.toFixed(1)}</div></div>
    <div class="series-card-publisher u-truncate">${p.read ? `${p.read}/${p.books} read` : 'unread'}</div>
  </div>`;
}

async function _loadPicks() {
  let d;
  try { d = await api.get('/api/ondeck/picks'); } catch { return; }
  if (currentView !== 'ondeck' || !d.picks.length) return;
  const slot = document.getElementById('od-picks');
  if (!slot) return;
  slot.innerHTML = `<div class="od-row picks-row">
    <div class="od-head"><span class="series-card-title">Community picks</span></div>
    <div class="series-grid">${d.picks.map(_pickCard).join('')}</div>
  </div>`;
}

// --- Readers like you (kometa/cbr_people.py): ComicBookRoundup's critics and readers
// who score your runs the way you do, and what else they score high. Series you
// don't have, so no cover yet: the card opens the catalogue sheet, FIND searches.
// Nothing harvested yet = no row at all; they turn up on their own as the data lands.
function _readerCard(it) {
  const title = it.year ? `${it.name} (${it.year})` : it.name;
  const sheet = jsArg({ title: it.name, publisher: it.publisher_name || it.publisher || '', year: it.year,
    why: [it.label] });
  // a pick matched to one of YOUR series (kometa/community_covers.py) opens it; the rest open the catalogue sheet
  const go = it.series_id ? `navigate('series-detail', {id: ${it.series_id}})` : `showCatalogueModal(${sheet})`;
  const src = !it.cover ? '' : it.cover.startsWith('http') ? `/api/img?u=${encodeURIComponent(it.cover)}` : it.cover;
  const cover = src ? `<img class="series-card-cover" src="${esc(src)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">`
                    : '<div class="series-card-cover rl-nocover"></div>';
  return `<div class="series-card rel-card${it.owned ? '' : ' rel-gap'}" tabindex="0" role="button" onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}" title="${esc(it.label || title)}">
    <div class="series-card-img-wrap">${cover}
      ${it.owned ? '' : `<button class="od-menu rel-get" title="Find it in the catalogue" onclick="event.stopPropagation(); showAddWizard(${jsArg(it.name)})">FIND</button>`}</div>
    <div class="series-card-footer"><div class="series-card-title">${esc(it.name)}</div>
      <div class="series-card-count" style="color:var(--tq)">${it.supporters}</div></div>
    <div class="series-card-publisher u-truncate">${esc(it.publisher_name || it.publisher || '')}</div>
  </div>`;
}

async function _loadReaders() {
  const [rows, all] = await Promise.all([
    api.get('/api/discover/community/because-ondeck?limit=12').catch(() => null),
    api.get('/api/discover/community?limit=12').catch(() => null)]);
  if (currentView !== 'ondeck') return;
  const row = (head, items) => `<div class="od-row readers-row">
    <div class="od-head"><span class="series-card-title">${head}</span></div>
    <div class="series-grid">${items.map(_readerCard).join('')}</div></div>`;
  const because = (rows && rows.rows || []).filter(r => (r.items || []).length);
  const a = document.getElementById('od-readers');
  if (a && because.length) a.innerHTML = because.map(r => row(`Because you're reading ${esc(r.seed.title)}`, r.items)).join('');
  const b = document.getElementById('od-likeyou');
  if (b && all && (all.items || []).length >= 4) b.innerHTML = row('Readers like you', all.items);
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
    <div class="series-card-img-wrap">${r.cover ? `<img class="series-card-cover" src="${esc(_ext(r.cover))}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
      <button class="od-menu rel-get" title="Get this" aria-label="Get" onclick="event.stopPropagation(); _rlGet(${r.list_id}, ${r.item_id}, this)">GET</button></div>
    <div class="series-card-footer"><div class="series-card-title">${esc(r.title)}</div>
      <div class="series-card-count"></div></div>
    <div class="series-card-publisher u-truncate rel-why" title="${esc(why)}">${esc(why)}</div>
  </div>`;
}

// A series you don't have at all, by someone whose work you've been reading:
// the catalogue's cover, and Track puts it on the shelf (pull list off).
function _catalogueCard(r) {
  const why = (r.why || []).join(' · ');
  const payload = jsArg({ metron_id: r.metron_series_id, title: r.title, year_began: r.year || null, on_pull_list: true });
  const open = `showCatalogueModal(${jsArg(r)})`;
  return `<div class="series-card rel-card rel-gap" tabindex="0" role="button" title="${esc(r.title)}${r.year ? ' (' + r.year + ')' : ''}" onclick="${open}" onkeydown="if(event.key==='Enter'||event.key===' ')${open}">
    <div class="series-card-img-wrap">${r.cover ? `<img class="series-card-cover" src="${esc(_ext(r.cover))}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">` : '<div class="series-card-cover rl-nocover"></div>'}
      <button class="od-menu rel-get" title="Track this series and put it on the pull list" aria-label="Track" onclick="event.stopPropagation(); _relTrack(${payload}, this)">TRACK</button></div>
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

// The Related row's shape while a run's credits are still being read: a new series
// waits for them (its issues are landing), and the row says so by being there, empty.
function _relSkeletons(n) {
  return Array.from({ length: n }, () => `<div class="series-card rel-card skeleton"><div class="series-card-img-wrap"></div><div class="series-card-footer"><div class="series-card-title"></div></div></div>`).join('');
}

function _paintRelated(id, d, attempt = 0) {
  if (currentView !== 'series-detail' || currentParams.id !== id) return;
  // the catalogue is being asked in the background: look again. A new run's credits come
  // off Metron's queue in two to four minutes (Absolute Evil two, Dark Wolverine four,
  // 2026-10-17); four looks over 16 s left the placeholder up for good. Leaving stops it.
  if (d.pending && attempt < 24) setTimeout(() => _loadRelated(id, attempt + 1), attempt < 4 ? 4000 : 15000);
  const app = document.getElementById('app');
  // one row per weighty credit — "More from Paul Pope" — the name is the reason
  const creators = d.creators || [];
  const sig = { p: !!d.pending, r: d.related.map(x => x.series_id), c: creators.map(r => [r.creator_id, r.items.map(i => i.series_id || i.metron_series_id)]) };
  if (_rowUnchanged('.rel-row', sig)) return;
  app.querySelector('.rel-row')?.remove();
  if (!d.related.length && !creators.length && !d.pending) return;
  app.insertAdjacentHTML('beforeend', `<div class="od-row rel-row">
    <div class="od-head"><span class="series-card-title">Related</span></div>
    ${d.related.length ? `<div class="series-grid">${d.related.map(_relCard).join('')}</div>`
      : d.pending ? `<div class="series-grid" aria-hidden="true">${_relSkeletons(6)}</div>` : ''}
    ${creators.map(r => `<div class="od-head" style="margin-top:14px"><span class="series-card-title">More from ${esc(r.name)}</span></div>
      <div class="series-grid">${r.items.map(_relCard).join('')}</div>`).join('')}
  </div>`);
  _rowSig(app.querySelector('.rel-row'), sig);
}

// The run's specials — annuals, one-shots, tie-ins the catalogue files as their
// own series (kometa/family.py). Their own pages, their own dates; shown here so
// the Library can fold them under the run.
function _paintSpecials(s) {
  const app = document.getElementById('app');
  app.querySelector('.specials-row')?.remove();
  app.insertAdjacentHTML('beforeend', `<div class="od-row specials-row">
    <div class="od-head"><span class="series-card-title">Specials</span></div>
    <div class="series-grid">${s.family_children.map(c => `
      <div class="series-card rel-card" tabindex="0" role="button" onclick="navigate('series-detail', {id: ${c.id}})" title="${esc(c.title)}">
        <div class="series-card-img-wrap"><img class="series-card-cover" src="/api/series/${c.id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'">
          ${c.calendar_date ? `<div class="series-card-next-release">${_fmtReleaseDate(c.calendar_date)}</div>` : ''}</div>
        <div class="series-card-footer"><div class="series-card-title">${esc(c.title.replace(new RegExp('^' + s.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*[:\\-–]?\\s*', 'i'), '') || c.title)}</div>
          <div class="series-card-count" style="color:${c.owned && c.owned >= c.total ? 'var(--pri)' : 'var(--tq)'}">${c.owned}/${c.total}</div></div>
        <div class="series-card-publisher u-truncate">${esc(c.metron_type || '')}</div>
      </div>`).join('')}</div>
  </div>`);
}

// A credited person, tapped: everything by them. Shelf first (tap → series),
// then the catalogue (Track). Same lookup as the "More from" rows.
async function showCreatorModal(creatorId, name, attempt = 0) {
  let d;
  try { d = await api.get(`/api/creators/${creatorId}?name=${encodeURIComponent(name || '')}`); }
  catch { showToast('Couldn’t look that person up', 'error'); return; }
  const paint = () => `
    <div class="modal-header"><h2>${esc(d.name || name || 'Creator')}</h2>
      <div class="u-label" style="color:var(--tq)">${d.shelf.length} on your shelf · ${d.catalogue.length} in the catalogue${d.pending ? ' · still looking…' : ''}</div></div>
    <div class="modal-body creator-modal">
      ${(d.sections || []).map((sec, i) => `<div class="od-head"${i ? ' style="margin-top:14px"' : ''}><span class="series-card-title">${esc(sec.label)}</span></div>
        <div class="series-grid">${sec.items.map(_relCard).join('')}</div>`).join('')}
      ${!d.shelf.length && !d.catalogue.length ? `<div class="od-empty">${d.pending ? 'Asking the catalogue…' : 'Nothing else by them that Metron knows.'}</div>` : ''}
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div>`;
  if (attempt === 0) document.getElementById('modal').classList.add('modal-wide');
  else if (!document.querySelector('#modal .creator-modal')) return;      // they closed it: stop looking
  showModal(paint());
  if (d.pending && attempt < 2) setTimeout(() => showCreatorModal(creatorId, name, attempt + 1), 4000);
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
  const sig = d.rows.map(r => [r.anchor_id, r.items.map(i => i.series_id || i.metron_series_id || i.title)]);
  if (_rowUnchanged('.sug-row', sig)) return;
  app.querySelectorAll('.sug-row').forEach(e => e.remove());
  const slot = document.getElementById('od-because');
  const anchor = app.querySelector('.trend-row');
  for (const r of d.rows) {
    const html = `<div class="od-row sug-row">
      <div class="od-head"><span class="series-card-title">Because you read ${esc(r.anchor)}</span></div>
      <div class="series-grid">${r.items.map(_relCard).join('')}</div>
    </div>`;
    if (slot) slot.insertAdjacentHTML('beforeend', html);
    else if (anchor) anchor.insertAdjacentHTML('beforebegin', html); else app.insertAdjacentHTML('beforeend', html);
  }
  _rowSig(app.querySelector('.sug-row'), sig);
}

// One ⋯ per card, one sheet: Read, the series behind it, read state. No Close (outside
// click and Esc do it) and no Not now (2026-10-13). The card's ✕ is Mark as read (10-14).
// Same shape as the reader's own ⋯ — tap the thing, get its actions.
function _bookActions(c) {
  // A tracked series' issue: the series page's own modal, cover and details and
  // all, with the card's actions in its footer. A trade or an untracked shelf
  // book has no issue to look up, so it gets the same modal shape built from
  // the book itself (cover, pages, star, rating) — never a bare list of buttons.
  if (c.series_id && c.number != null) return showIssueModal(c.series_id, c.number, { book: c });   // not in the run's list: the book's card
  return showBookModal(c);
}

// The issue modal's shape for a book that isn't an issue: trades, one-shots,
// untracked shelf books. Same cover column, same footer slots.
// Mark as read / unread on a card: the reader's own when the card is over it (Mark as read
// there is the end screen), the plain flip anywhere else.
function _readStateCall(bookId, done, inReader) {
  if (inReader) return done ? '_rdMarkUnread()' : '_rdMarkRead()';
  return `_bookSetRead(${bookId}, ${done ? 'false' : 'true'})`;
}

async function showBookModal(c, opts = {}) {
  let bk = null;
  try { bk = await api.get(`/api/books/${c.book_id}`); } catch {}
  const label = c.label || '';
  const series = c.series || bk?.title || '';
  // a trade's file name already carries the series: don't say it twice
  const title = label && series && label.toLowerCase().startsWith(series.toLowerCase().replace(/\s*\(\d{4}\)$/, '')) ? label : `${series} ${label}`.trim();
  const completed = bk?.progress?.completed ?? c.completed;
  const seriesId = c.series_id || bk?.series_id || null;          // the book knows its run even when the card didn't
  const goSeries = seriesId ? `_wantTab = 'trades'; navigate('series-detail', {id: ${seriesId}})`
    : c.shelf_id && currentView !== 'shelf' ? `navigate('shelf', {id: ${c.shelf_id}})` : '';
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
        <div class="issue-modal-details" id="issue-modal-details"></div>
      </div>
    </div>
    <div class="modal-footer">
      ${_wantBtnHtml({ book_id: c.book_id })}
      ${goSeries ? `<button class="btn btn-ghost" onclick="closeModal(); ${goSeries}">Go to series</button>` : ''}
      <button class="btn btn-ghost" onclick="closeModal(); ${_readStateCall(c.book_id, completed, opts.inReader)}">${completed ? 'Mark as unread' : 'Mark as read'}</button>
      ${opts.inReader ? '' : `<button class="btn btn-primary" onclick="closeModal(); navigate('read', {book: ${c.book_id}})">Read</button>`}
    </div>`);
  // a trade the catalogue lists: the book sites' ratings under the stars
  try {
    const r = await api.get(`/api/books/${c.book_id}/community`);
    if (r.community) _renderIssueDetails('', [], r.community);
  } catch {}
}

// A series that isn't on the shelf (Related's "By the same people", Trending):
// the same modal, the catalogue's facts, and Track where Read would be.
function showCatalogueModal(r) {
  const why = (r.why || []).filter(Boolean);
  const anchor = r.metron_series_id ? { metron_id: r.metron_series_id } : r.locg_series_id ? { locg_id: r.locg_series_id } : null;
  const payload = jsArg({ ...anchor, title: r.metron_title || r.title, publisher_name: r.publisher || '',
    year_began: r.year || null, on_pull_list: true });
  document.getElementById('modal').classList.add('modal-wide');
  showModal(`
    <div class="issue-modal-layout">
      ${_modalCoverHtml(_ext(r.cover || ''), r.title)}
      <div class="issue-modal-info">
        <div class="issue-modal-num">${esc(r.title)}</div>
        <div class="issue-modal-series">${esc([r.publisher, r.year].filter(Boolean).join(' · '))}</div>
        <div style="margin:8px 0"><span class="chip" style="color:var(--tq);border-color:var(--tq)">Not on the shelf</span>
          ${r.total ? `<span class="chip chip-neutral">${r.total} issues</span>` : ''}${r.rank ? `<span class="chip chip-neutral">#${r.rank} this month</span>` : ''}</div>
        ${why.length ? `<div class="issue-modal-meta">${why.map(esc).join(' · ')}</div>` : ''}
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      ${anchor ? `<button class="btn btn-primary" onclick="closeModal(); _relTrack(${payload}, this)">Track series</button>`
        : `<button class="btn btn-primary" onclick="closeModal(); showAddWizard(${jsArg(r.title)})">Find in catalogue</button>`}
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

// The ✕ on a Continue reading tile = Mark as read (2026-10-14: no more 'Not now' —
// a book leaves Continue reading by being read). Undo on the toast puts your place
// back exactly where it was, not on page 1.
async function _odMarkRead(bookId, page) {
  const tile = document.getElementById(`od-${bookId}`);
  const stamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  try {
    const b = await api.get(`/api/books/${bookId}`);
    await api.put(`/api/books/${bookId}/progress`, { page: b.page_count || 1, completed: true, updated_at: stamp() });
  } catch (e) { showToast('Couldn\u2019t change that', 'error'); return; }
  await _animateTileOut(tile);
  showToastAction('Marked as read', 'Undo', async () => {
    try { await api.put(`/api/books/${bookId}/progress`, { page, completed: false, updated_at: stamp() }); }
    catch { showToast('Couldn\u2019t undo that', 'error'); return; }
    if (currentView === 'ondeck') renderOnDeck();
  });
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
      // read every "after" rect BEFORE the first write: a read after a write forces a
      // synchronous layout, and this loop used to do that once per tile
      const after = new Map(others.map(el => [el, el.getBoundingClientRect()]));
      for (const el of others) {
        const a = before.get(el), b = after.get(el);
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
  const all = (await _libFetch()).series;
  const rows = all.filter(_needsMatch).sort((a, b) =>
    (a.match_status === 'needs_match' ? 0 : 1) - (b.match_status === 'needs_match' ? 0 : 1)
    || a.title.localeCompare(b.title));
  let sourcing = [];
  try { sourcing = (await api.get('/api/sourcing/candidates')).items || []; } catch {}
  _updateNeedsBadge(rows.length + sourcing.length);
  if (currentView !== 'needs-match') return;
  if (!rows.length && !sourcing.length) {
    setApp(`<div class="empty-state"><div class="empty-state-title">Everything's matched</div>
      <div style="margin-top:8px;color:var(--tq);font-size:13px">New folders land here until they're matched to a run.</div></div>`);
    return;
  }
  const waiting = rows.filter(s => s.match_status === 'pending').length;
  _nmRows = rows;
  _sourcingRows = sourcing;
  setApp(`
    ${rows.length ? `
      <div class="nm-intro">${rows.length} folder${rows.length === 1 ? '' : 's'} not yet matched to a run${waiting
        ? ` — ${waiting} still in the queue` : ''}.
        <a class="btn-link" style="margin-left:10px" onclick="navigate('singles')">Shelf clean-up ${ico('right')}</a></div>
      <input class="browse-search nm-search" id="nm-search" type="text" placeholder="Search titles, publishers, folders"
        value="${esc(_nmQuery)}" oninput="_nmFilter(this.value)" autocomplete="off" spellcheck="false">
      <div class="nm-list" id="nm-list">${_nmListHtml()}</div>` : ''}
    ${sourcing.length ? `
      <div class="singles-group" style="${rows.length ? 'margin-top:32px;' : ''}">
        <div class="singles-head">
          <span class="series-card-title">Is this the same comic? <span class="settings-opt">${sourcing.length}</span></span>
        </div>
        <div class="nm-list">${sourcing.map((item, i) => _sourcingRowHtml(item, i)).join('')}</div>
      </div>` : ''}`);
  if (_nmQuery && rows.length) document.getElementById('nm-search')?.focus();
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
  if (currentView !== 'singles') return;
  if (_tabHidden()) { _collectionsPoll = setTimeout(_loadCollections, 10000); return; }
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
      line.innerHTML = x.by_name
        ? `<span style="color:var(--amb)">by name</span> · ${esc(x.parent_title)}`
        : `collects <b>${esc(x.collects_summary)}</b>${x.year ? ` · ${x.year}` : ''}${link}`;
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

function _sourcingRowHtml(s, i) {
  const go = `navigate('series-detail', {id: ${s.id}})`;
  const cands = s.candidates || [];
  const candListHtml = cands.length ? cands.map(c => `
    <div class="sourcing-cand-card" style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-subtle, rgba(255,255,255,0.03));border:1px solid var(--border-subtle, rgba(255,255,255,0.08));border-radius:6px;padding:6px 10px;margin-top:4px">
      <div class="sourcing-cand-info u-truncate" style="margin-right:12px">
        <span style="font-weight:600;color:var(--tx)">${esc(c.title)}</span>
        <span class="u-label" style="margin-left:6px;color:var(--tq)">${esc([c.year, c.issue_count != null ? `${c.issue_count} issue${c.issue_count === 1 ? '' : 's'}` : null].filter(Boolean).join(' · '))}</span>
        <span class="nm-status ${c.confidence === 'high' ? 'green' : 'amber'}" style="margin-left:8px;font-size:10px;padding:1px 6px">${c.confidence === 'high' ? 'High' : 'Moderate'}</span>
      </div>
      <button class="btn btn-primary btn-sm" onclick="_sourcingConfirm(${s.id}, ${c.id})">Confirm</button>
    </div>
  `).join('') : '<div class="tidy-why" style="margin-top:4px">No confident Metron match found.</div>';

  return `
    <div class="nm-row card-cascade" id="src-${s.id}" style="animation-delay:${Math.min(i,14)*STAGGER_MS}ms">
      <img class="nm-cover" src="/api/series/${s.id}/thumbnail" alt="" loading="lazy" onerror="this.style.opacity='0.15'" onclick="${go}">
      <div class="nm-main">
        <div class="nm-title" onclick="${go}" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">${esc(s.title)}</div>
        <div class="nm-meta u-truncate">${s.publisher ? esc(s.publisher) + ' · ' : ''}${s.year ? esc(s.year) + ' · ' : ''}${s.total_issues} issue${s.total_issues === 1 ? '' : 's'}${s.folder_path ? ' · ' + esc(s.folder_path.replace(/^\/comics\//, '')) : ''}</div>
        <div style="margin-top:6px">${candListHtml}</div>
      </div>
      <div class="nm-actions" style="display:flex;flex-direction:column;gap:6px;align-self:flex-start">
        <button class="btn btn-ghost btn-sm" onclick="_sourcingDifferentId(${s.id})">Enter different ID</button>
        <button class="btn btn-ghost btn-sm" onclick="_sourcingDecline(${s.id})">Decline</button>
      </div>
    </div>`;
}

async function _sourcingConfirm(seriesId, metronId) {
  try {
    await api.post('/api/sourcing/confirm', { series_id: seriesId, metron_series_id: metronId });
    closeModal();
    showToast('Metron series linked — credits and recommendations will populate');
    const el = document.getElementById(`src-${seriesId}`);
    if (el) el.remove();
    _sourcingRows = (_sourcingRows || []).filter(r => r.id !== seriesId);
    _updateNeedsBadge((_nmRows || []).length + _sourcingRows.length);
    if (!_sourcingRows.length && !(_nmRows || []).length) renderNeedsMatch();
  } catch (e) {
    showToast(`Couldn’t link Metron series: ${e.message || e}`, 'error');
  }
}

async function _sourcingDecline(seriesId) {
  try {
    await api.post('/api/sourcing/decline', { series_id: seriesId });
    showToast('Declined credit sourcing');
    const el = document.getElementById(`src-${seriesId}`);
    if (el) el.remove();
    _sourcingRows = (_sourcingRows || []).filter(r => r.id !== seriesId);
    _updateNeedsBadge((_nmRows || []).length + _sourcingRows.length);
    if (!_sourcingRows.length && !(_nmRows || []).length) renderNeedsMatch();
  } catch (e) {
    showToast(`Couldn’t decline: ${e.message || e}`, 'error');
  }
}

function _sourcingDifferentId(seriesId) {
  const s = (_sourcingRows || []).find(r => r.id === seriesId) || {};
  showModal(`
    <div class="modal-header"><h2>Enter Metron series ID</h2></div>
    <div class="modal-body">
      <div class="tidy-why" style="margin-bottom:10px"><b>${esc(s.title || 'this series')}</b>${s.publisher ? ` (${esc(s.publisher)})` : ''}${s.year ? ` · ${s.year}` : ''}${s.total_issues ? ` · ${s.total_issues} issues` : ''}</div>
      <input class="browse-search" id="sourcing-diff-input" type="text" placeholder="Metron Series ID (e.g. 13649) or URL" style="width:100%" onkeydown="if(event.key==='Enter')_sourcingPreviewId(${seriesId})">
      <div id="sourcing-diff-preview" style="margin-top:12px"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="sourcing-diff-btn" onclick="_sourcingPreviewId(${seriesId})">Preview</button>
    </div>`);
}

async function _sourcingPreviewId(seriesId) {
  const input = document.getElementById('sourcing-diff-input');
  if (!input) return;
  const raw = input.value.trim();
  const m = raw.match(/(\d+)/);
  if (!m) {
    showToast('Please enter a valid numeric Metron series ID', 'error');
    return;
  }
  const mid = parseInt(m[1], 10);
  const prevBox = document.getElementById('sourcing-diff-preview');
  const btn = document.getElementById('sourcing-diff-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Looking up…'; }
  if (prevBox) prevBox.innerHTML = '<div class="tidy-why">Checking Metron…</div>';
  try {
    const data = await api.get(`/api/sourcing/preview/${mid}`);
    if (btn) { btn.disabled = false; btn.textContent = 'Preview'; }
    if (!prevBox) return;
    prevBox.innerHTML = `
      <div class="sourcing-cand-card" style="background:var(--bg-subtle, rgba(255,255,255,0.03));border:1px solid var(--border-subtle, rgba(255,255,255,0.08));border-radius:6px;padding:10px;margin-top:6px">
        <div style="font-weight:600;font-size:14px;color:var(--tx)">${esc(data.title)}</div>
        <div class="u-label" style="color:var(--tq);margin-top:4px">${esc([data.publisher, data.year, data.issue_count != null ? `${data.issue_count} issues` : null].filter(Boolean).join(' · '))}</div>
        <div style="margin-top:8px">
          <button class="btn btn-primary btn-sm" onclick="_sourcingConfirm(${seriesId}, ${data.id})">Confirm this match</button>
        </div>
      </div>`;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Preview'; }
    if (prevBox) prevBox.innerHTML = `<div class="tidy-why" style="color:var(--amb)">Lookup failed: ${esc(e.message || String(e))}</div>`;
  }
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
      ${p.ops.filter(o => o.kind !== 'rename_folder').map(o => row(o.from, o.to, o.kind === 'convert' ? `cbr${ico('right')}cbz` : 'rename')).join('')}
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

// Search lives in the Library (its own box): '/' jumps to it while you're there.
document.addEventListener('keydown', e => {
  if (e.key !== '/' || currentView !== 'library' || e.target.closest('input, textarea, [contenteditable]')) return;
  e.preventDefault();
  document.getElementById('browse-search')?.focus();
});

function browseSearch(val) {
  clearTimeout(browseState.searchTimer);
  browseState.searchTimer = setTimeout(async () => {
    browseState.search = val;
    _renderBrowseResults();                       // titles answer at once
    if (val.trim().length < 3) { browseState.people = null; return; }
    // then the people behind the books (kometa/people.py): writers, artists, covers
    let d = null;
    try { d = await api.get(`/api/search/people?q=${encodeURIComponent(val)}`); } catch {}
    if (browseState.search !== val) return;       // typed on: a newer search owns the page
    browseState.people = d ? { q: val, people: d.people || [], why: d.series || {} } : null;
    _renderBrowseResults();
  }, 300);
}

// A person found by the search. With a catalogue id they have a page of their
// own; a name off a file's ComicInfo doesn't, so it narrows the Library to them.
function _personPick(p) {
  if (p.id) return showCreatorModal(p.id, p.name);
  const box = document.querySelector('.browse-search input, #browse-search');
  if (box) box.value = p.name;
  browseSearch(p.name);
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
    : covered ? `<span class="chip chip-collected">${ico('diamond')} Covered by a trade</span>`
    : '<span class="chip chip-missing">Missing</span>';
  const num = `#${esc(String(i.number ?? '?'))}`;
  const detailText = `This issue lives in its own run — <b>${esc(i.source_title || '')}</b>. Open it there for full
       details, variants${owned ? ' and to read it' : ' and to get it'}.`;
  const footerAction = `<button class="btn btn-primary" onclick="closeModal();openArcIssue(${s.id}, ${readingOrder})">Open in ${esc(i.source_title || 'its run')} ${ico('right')}</button>`;
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

// The ★ sort only exists for the run whose scores are loaded. Anywhere else
// (a run you don't own, one nobody rated) it quietly falls back to numbers.
const _ratedHere = s => detailSortRated && _topRated && s && _topRated.id === s.id ? _topRated.by : null;

function buildIssueTiles(s) {
  const by = _ratedHere(s);
  const filtered = s.issues.filter(i => {
    const st = issueStatus(i);
    if (detailTab === 'owned')    return st === 'owned';
    if (detailTab === 'missing')  return st === 'missing' || st === 'today';  // out-today + unowned is still missing
    if (detailTab === 'upcoming') return st === 'upcoming';
    return true;
  }).sort((a, b) => by ? ((by[b.number] ?? -1) - (by[a.number] ?? -1)) || a.number - b.number
    : detailSortDesc ? b.number - a.number : a.number - b.number);

  return filtered.map(issue => _issueTileHtml(s, issue)).join('');
}

// The ⋯ on an issue you have: its card, while a tap reads it — the same on every
// screen, and in the apps, where a long-press is the ⋯ (2026-10-17). One you don't
// have needs no ⋯: a tap opens its card.
function _cardDots(seriesId, number) {
  return `<button class="od-menu" title="Issue details" aria-label="Issue details" onclick="event.stopPropagation(); showIssueModal(${seriesId}, ${number})">${DOTS}</button>`;
}

function _issueTileHtml(s, issue) {
    const st = issueStatus(issue);
    const num = `#${fmtNum(issue.number)}`;
    const score = _ratedHere(s)?.[issue.number];
    const dateBadge = st === 'today'
      ? `<div class="series-card-next-release">TODAY</div>`
      : (st === 'upcoming' && issue.store_date
          ? `<div class="series-card-next-release">${issue.store_date.replace(/-/g, '/')}</div>`
          : '');
    let inner = '';
    if (st === 'owned') {
      // variant_cover (your chosen cover) wins; else the issue route serves the file's own
      const thumbSrc = issue.variant_cover ? issue.variant_cover : bookThumb(issue);
      inner = `<div class="issue-tile-img">
        <img src="${esc(thumbSrc)}" alt="${num}" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">
        ${_cardDots(s.id, issue.number)}
      </div>`;
    } else {
      // variant_cover = your saved variant pick (not-yet-downloaded issues) — show it
      // here too so the grid tile matches the modal, falling back to the solicit cover.
      // No client-side art at all? Ask the server — its thumbnail route walks the
      // whole fallback chain (LOCG main → variant art). An issue with
      // genuinely zero art anywhere 404s and the img removes itself, same blank as before.
      const thumbSrc = issue.variant_cover
        || _metronArt(issue)
        || `/api/series/${s.id}/issues/${issue.number}/thumbnail?c=3`;   // c: Comic Vine covers for artless issues (2026-10-18)
      inner = `<div class="issue-tile-img ${st}">
        <img src="${esc(thumbSrc)}" alt="${num}" loading="lazy"
          onerror="this.remove()">${dateBadge}
      </div>`;
    }
    const searchBtn = (st === 'missing' || st === 'today')
      ? `<button class="issue-tile-search" title="Get this issue" aria-label="Get issue ${fmtNum(issue.number)}" data-dl="${s.id}:${issue.number}"
           onclick="event.stopPropagation(); searchIssue(${s.id}, ${issue.number}, this)">↓</button>`
      : '';
    const go = st === 'owned' ? `openIssueReader(${s.id}, ${issue.number})` : `showIssueModal(${s.id}, ${issue.number})`;
    return `<div class="issue-tile" data-num="${issue.number}" title="${esc(s.title)} ${num}" tabindex="0" role="button"
      onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
      ${inner}
      <div class="issue-tile-num">${num}${score != null
        ? ` <span class="comm-v${score >= 7.5 ? ' hi' : score < 6 ? ' lo' : ''}">${score.toFixed(1)}</span>` : ''}</div>
      ${searchBtn}
    </div>`;
}

// [title, label] for the sort button: ↓ # → ↑ # → ★ (when there's a ★ to give)
function _sortBtnState() {
  if (_ratedHere(_detailSeries)) return ['Top rated', ico('star')];
  return detailSortDesc ? ['Newest first', '↓ #'] : ['Oldest first', '↑ #'];
}

function cycleIssueSort(id) {
  const canRate = _topRated && _topRated.id === id;
  if (_ratedHere(_detailSeries)) { detailSortRated = false; detailSortDesc = true; }
  else if (!detailSortDesc && canRate) detailSortRated = true;
  else { detailSortRated = false; detailSortDesc = !detailSortDesc; }
  flipIssueSort(id);
}

function flipIssueSort(id) {
  const btn = document.querySelector('.sort-toggle');
  if (btn) [btn.title, btn.innerHTML] = _sortBtnState();
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

// Arc ownership is a resolved SNAPSHOT (matched against the shelf), not a
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
  const collChip = s.collected ? `<span class="chip chip-collected" title="${esc(s.collection?.name || '')}">${ico('diamond')} collected</span>` : '';
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
    const covBadge = covered ? `<div class="issue-tile-cov">${ico('diamond')}</div>` : '';
    const cover = i.image_url
      ? `<img src="${esc(i.image_url)}" alt="${num}" loading="lazy" onerror="this.remove()">`
      : '';
    const ttl = `${esc(i.source_title || '')} ${num}${i.story_title ? ' — ' + esc(i.story_title) : ''}`;
    // The REAL issue modal (showIssueModal) — same one every series page uses, same
    // Download button, variants, LOCG details — scoped to the row's OWN home series
    // (tracked_series_id, resolved server-side). Falls back to the thin arc-only
    // modal only in the rare case a participating run somehow isn't resolved yet.
    // One you have reads on a tap, its ⋯ opens the card (_cardDots).
    const home = i.tracked_series_id;
    const openIssue = home
      ? (owned ? `openIssueReader(${home}, ${Number(i.number)})` : `showIssueModal(${home}, ${Number(i.number)})`)
      : `showArcIssueModal(${i.reading_order})`;
    return `<div class="issue-tile" title="${ttl}" tabindex="0" role="button"
      onclick="${openIssue}"
      onkeydown="if(event.key==='Enter'||event.key===' ')${openIssue}">
      <div class="issue-tile-img${stateCls}">${cover}${covBadge}${home && owned ? _cardDots(home, Number(i.number)) : ''}</div>
      <div class="issue-tile-num">${xt}${num}</div>
    </div>`;
  }).join('');

  const collBanner = (s.collected && s.collection) ? `
    <div class="arc-collected-note" tabindex="0" role="button" onclick="navigate('series-detail',{id:${s.collection.series_id}})"
      onkeydown="if(event.key==='Enter'||event.key===' ')navigate('series-detail',{id:${s.collection.series_id}})">
      ${ico('diamond')} Owned as a collected edition — <strong>${esc(s.collection.name)}</strong>. Read it from the collected edition.
    </div>` : '';
  const body = total
    ? `<div class="issue-grid">${tiles}</div>`
    : `<div class="state-msg" style="padding:28px 0;font-size:12px;color:var(--tq)">Pulling reading order from ComicVine…</div>`;
  document.getElementById('topbar-title').textContent = s.title;
  document.getElementById('topbar-chips').innerHTML = chips;
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-primary btn-sm" id="arc-fulfill-btn" onclick="confirmFulfillArc(${s.id})">Get this storyline</button>
    <button class="btn btn-ghost btn-sm" onclick="buildArcReadlist(${s.id}, this)">Make reading list</button>
    <button class="btn btn-ghost btn-sm" onclick="refreshArcOwnership(${s.id}, this)">Refresh ownership</button>
  `;
  document.getElementById('topbar-sub').innerHTML =
    `<span class="series-meta-text u-truncate">${ico('diamond')} storyline · issues live in their own runs${primary ? ` · originates in ${esc(primary)}` : ''}</span>`;

  setApp(`
    <div class="issue-tabs-row">${originId ? `<div class="issue-tab active" tabindex="0" role="button"
        onclick="detailTab='arcs';navigate('series-detail',{id:${originId}})"
        onkeydown="if(event.key==='Enter'||event.key===' '){detailTab='arcs';navigate('series-detail',{id:${originId}})}">
        ${ico('left')} Back to ${esc(originTitle)} Arcs</div>` : ''}</div>
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
    showToast(`Reading list "${r.name}" · ${r.owned} of ${r.entries} on the shelf`);
    navigate('readlist', { id: r.list_id });
  } catch (e) {
    showToast('Reading list failed — ' + (e?.message || e), 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Make reading list'; }
  }
}

async function refreshArcOwnership(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Checking the shelf…'; }
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
  // Re-rendering the series you're already on (a toggle, a tab, a rating) must
  // not blank the page: keep what's there until the new paint lands, and put
  // the scroll back where it was. Only a NEW series shows the loading state.
  const sameSeries = _detailSeries?.id === id && currentView === 'series-detail';
  // #app is the scroll container (body doesn't scroll): window.scrollY was always 0,
  // so every same-series re-render (a tab, a toggle, a rating) jumped to the top
  const keepScroll = sameSeries ? (document.getElementById('app')?.scrollTop || 0) : 0;
  if (!sameSeries) setApp('<div class="state-msg">Loading...</div>');

  const s = await api.get(`/api/series/${id}`);
  _detailSeries = s;
  // Ask for Related NOW, before the grid's hundred cover requests queue up in
  // front of it — the row paints once the grid is there.
  const relP = api.get(`/api/series/${id}/related`).catch(() => null);
  if (s.kind === 'arc') return renderArcDetail(s);
  // Owned runs only: the community's scores, already in the record — a local read
  if (s.owned > 0) {
    const tr = await api.get(`/api/series/${id}/top-rated`).catch(() => null);
    _topRated = tr && tr.issues.length ? { id, by: Object.fromEntries(tr.issues.map(i => [i.number, i.score])) } : null;
  } else _topRated = null;

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
  if (_wantTab) { detailTab = _wantTab; _wantTab = null; _autoTabFor = id; }

  const chips = [
    released > 0 ? `<span class="chip ${s.owned < released ? 'chip-missing' : 'chip-complete'}">${s.owned}/${released}</span>` : '',
    // ONE status chip: what's coming when something is solicited, otherwise whether
    // the run is still going (kometa/runstatus.py — Metron's word checked against our dates)
    s.upcoming ? `<span class="chip chip-upcoming">${s.upcoming} upcoming</span>`
      : s.run_status ? `<span class="chip chip-upcoming" title="${esc(s.run_status.why)}">${esc(s.run_status.label)}</span>` : '',
    s.from_list_id ? `<a class="chip chip-collected" title="Tracked by a reading list's Get — pull list off, only what the list asked for" onclick="navigate('readlist', {id: ${s.from_list_id}})">${ico('diamond')} from a reading list</a>` : '',
    s.family_parent ? `<a class="chip chip-neutral" title="A special of this run" onclick="navigate('series-detail', {id: ${s.family_parent.id}})">part of ${esc(s.family_parent.title)}</a>` : '',
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
  // Build candidates off the SAME cover chain the grid tiles use (the file's own cover
  // for an owned issue → catalogue art → per-issue thumbnail route), not the raw
  // metron_image column. LOCG-only series (e.g. one-shots) never stamp metron_image, so
  // keying off it alone left them with a blank backdrop. The per-issue route self-heals.
  const _covers = (s.issues || []).map(i =>
    i.owned ? bookThumb(i)
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
    ${_wantBtnHtml({ series_id: s.id }, 'btn-sm')}
    ${pullBtn}
    ${oversizedBtn}
    ${s.missing > 0 ? `<button class="btn btn-ghost btn-sm" onclick="sweepSeries(${s.id}, this)">Sweep Missing</button>` : ''}
    ${s.folder_path && s.match_status !== 'pending' && s.match_status !== 'needs_match'
      ? `<button class="btn btn-ghost btn-sm" title="Rename files and folder to the house convention (dry run first)" onclick="_tidyPlan(${s.id}, this)">Tidy</button>` : ''}
    <button class="btn btn-ghost btn-sm" onclick="confirmDelete(${s.id})">Remove</button>
  `;
  _wantSync(document.getElementById('topbar-actions'));
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
      <button class="btn-icon sort-toggle" title="${_sortBtnState()[0]}"
        onclick="cycleIssueSort(${id})">${_sortBtnState()[1]}</button>
    </div>
    ${detailTab === 'trades'
      ? `<div id="trades-panel" class="trades-body"><div class="state-msg" style="padding:20px 0;font-size:11px">Looking for trades…</div></div>`
      : detailTab === 'arcs'
      ? `<div id="arcs-panel" class="arcs-body"><div class="state-msg" style="padding:20px 0;font-size:11px">Looking for arcs…</div></div>`
      : `<div class="issue-grid">${tiles || (
          // A run tracked seconds ago: its issue list is still coming off the catalogue (30 s for
          // The Joker Presents: A Puzzlebox, 2026-10-16). Cover-sized skeletons where the tiles
          // will land, not a sentence — the poller below swaps them for the real grid.
          (s.locg_series_id || s.metron_series_id) && total === 0 && !s.last_synced && !s.has_trades
            ? Array.from({ length: 12 }, () => `<div class="issue-tile skeleton" aria-hidden="true"><div class="issue-tile-img"></div><div class="issue-tile-num"></div></div>`).join('')
            : `<div class="state-msg" style="grid-column:1/-1">${
          total === 0 && s.has_trades ? `No single issues — collected in Trades ${ico('right')}`
          : (s.locg_series_id || s.metron_series_id) && total === 0 ? 'No issues found for this series.'
          : 'Nothing here.'}</div>`)}</div>`}
  `);

  if (detailTab === 'trades') _loadTradesPanel(id);
  if (keepScroll) requestAnimationFrame(() => { const a = document.getElementById('app'); if (a) a.scrollTop = keepScroll; });
  if (detailTab === 'arcs') _loadArcsPanel(id);
  if (s.match_status === 'needs_match' || s.match_status === 'pending') _loadMatchCandidates(id, s.title);
  if (s.match_status === 'pending' || s.match_status === 'needs_match') _showLocgPause(id);
  if (s.shelf_id && (detailTab === 'all' || detailTab === 'owned')) _loadShelfFiles(s, total === 0);
  if (detailTab === 'all' && s.family_children?.length) _paintSpecials(s);
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
  if ((s.locg_series_id || s.metron_series_id) && total === 0 && !s.has_trades && detailTab !== 'trades'
      && (!s.last_synced || _autoSynced.has(id)) && !_autoPollDone.has(id)) {
    const _syncedAt = s.last_synced || null;
    const _stopAt = Date.now() + 90000;
    const _pollId = _detailPollId = setInterval(async () => {
      if (currentView !== 'series-detail' || currentParams.id !== id) { clearInterval(_pollId); return; }
      if (_syncInFlight.has(id)) return;          // the sync's own poller is watching this series already
      const fresh = await api.get(`/api/series/${id}/status`).catch(() => null);
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
  const badge = `<div class="series-card-next-release">${ico('diamond', !tracked)} ARC</div>`;
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

async function _loadTradesPanel(id, attempt = 0) {
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
  if (data.pending && attempt < 30) {       // being looked up in the background: ask again shortly
    if (!body.querySelector('.trades-wait'))
      body.innerHTML = `<div class="state-msg trades-wait" style="padding:20px 0;font-size:11px;display:flex;gap:10px;align-items:center"><span class="act-job-spin" aria-hidden="true"></span>Looking up…</div>`;
    setTimeout(() => _loadTradesPanel(id, attempt + 1), 4000);
    return;
  }
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
  const ownedBadge = t.owned ? `<div class="trade-owned-check" title="On disk">${ico('check')}</div>` : '';
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
    // On disk, and the shelf knows the file → Kometa's own reader; else just the fact.
    if (t.book_id) {
      return `<button class="btn btn-primary" onclick="closeModal();navigate('read', {book: ${t.book_id}})">Read</button>`;
    }
    return `<span class="trade-owned-note">On disk</span>`;
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
    _renderIssueDetails(d.desc, d.credits || [], d.community);
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
    if (isArrow) btn.innerHTML = DOTS;
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
    if (btn) { if (isArrow) { btn.innerHTML = ico('check'); btn.classList.add('found'); } else btn.innerHTML = ico('check') + ' Queued'; }
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
              <div class="wizard-result-title u-truncate">${esc(r.series || r.name || '')}${r.kind === 'storyline' ? ` <span class="locg-badge">${ico('diamond')} STORYLINE</span>` : r.kind === 'arc' ? ` <span class="locg-badge">${ico('diamond')} ARC</span>` : _isCollectedResult(r) ? ` <span class="locg-badge collected-badge">${ico('diamond')} COLLECTED</span>` : r.needs_resolve ? ` <span class="locg-badge">${ico('diamond')} ONE-SHOT</span>` : r.source === 'locg' ? ' <span class="locg-badge">LOCG</span>' : ''}</div>
              <div class="wizard-result-meta">${r.kind === 'storyline' ? `story arc · originates in ${esc(r.origin_title || '?')}${r.origin_year ? ' (' + r.origin_year + ')' : ''}` : `${esc(r.publisher?.name || '')}${r.kind === 'arc' ? ' · story arc' : r.needs_resolve ? ` · one-shot ${ico('right')} its series` : _isCollectedResult(r) ? ' · collected edition — lives in a series’ Trades' : ''}${r.year_began ? ' · ' + r.year_began : ''}${r.issue_count ? ' · ' + r.issue_count + ' issues' : ''}`}</div>
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

    // Metron is the primary catalogue (since 2026-10-08) and this search never
    // asked it — 'shadow of the bat' found nothing because LOCG doesn't index the
    // phrase. Metron leads; LOCG fills in what Metron lacks; a shut LOCG costs nothing.
    const metP = api.get(`/api/search/metron?q=${encodeURIComponent(q)}`).catch(() => []);
    const locgResults = await api.get(`/api/search/locg?q=${encodeURIComponent(q)}`).catch(() => []);
    const metron = await metP;
    if (seq !== _wizardSeq || !document.getElementById('wizard-results')) return;
    const runKey = r => `${(r.series || '').toLowerCase().replace(/^the\s+/, '').replace(/[^a-z0-9]+/g, '')}|${r.year_began || ''}`;
    const seen = new Set(metron.map(runKey));
    const runs = [...metron, ...locgResults.filter(r => !seen.has(runKey(r)))];
    if (runs.length) {
      const arcs = (await cvP).filter(r => r.kind === 'arc');
      _renderWizardResults([...(await slP), ...arcs, ...runs], q); return;
    }

    // Nobody has a run — show the full ComicVine result (arcs + collected-edition volumes).
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
      <div class="modal-footer"><button class="btn btn-ghost" onclick="showAddWizard()">${ico('left')} Back</button></div>`;
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
    <div class="wizard-arc-note">${ico('diamond')} Story arc — Kometa tracks the reading order across every participating title. It owns no folder; the collected edition lands in its main series' Trades.</div>`
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
      <img class="wizard-result-thumb" src="${esc(r.cover || r.image || '')}" alt="" loading="lazy" onerror="this.style.opacity=0">
      <div class="wizard-result-text">
        <div class="wizard-result-title u-truncate">${esc(r.series || r.name || '')}${isArc ? ` <span class="locg-badge">${ico('diamond')} ARC</span>` : ''}</div>
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
      <button class="btn btn-ghost" onclick="showAddWizard()">${ico('left')} Back</button>
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
      hint.innerHTML = res.exists ? ico('check') + ' Existing folder — owned issues will be detected' : 'New folder — will be created on first download';
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
        <div class="wizard-result-title u-truncate">${esc(r.series || '')} <span class="locg-badge">${ico('diamond')} STORYLINE</span></div>
        <div class="wizard-result-meta">${esc(r.origin_publisher || '')}${r.origin_publisher ? ' · ' : ''}originates in ${run}</div>
      </div>
    </div>
    <div class="wizard-arc-note">Following adds <b>${run}</b> as a tracked series — its Issues, Trades and Arcs tabs. <b>Nothing downloads;</b> this is tracking only. The storyline then lives under that series' Arcs tab.</div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="showAddWizard()">${ico('left')} Back</button>
      <button class="btn btn-primary" id="wizard-add-btn" onclick="wizardConfirm()">Follow ${ico('right')} add ${esc(r.origin_title || 'series')}</button>
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
      btn.innerHTML = `Follow ${ico('right')} add ${esc(_wizardState.originTitle || 'series')}`;  // restore the real label, not a stub
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
    if (_tabHidden()) { setTimeout(tick, 10000); return; }     // lid closed: ask again in a while, not every 2.5 s
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
      showToast(`${label} downloaded`);
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
      else if (btn) { btn.innerHTML = ico('check'); btn.title = 'Downloaded'; btn.disabled = true; }
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
        ? 'The folder moves to the bin and is deleted for good after 7 days — Undo on the toast until then. To stop fetching a series but keep what you have, turn its pull list off instead.'
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
  if (e.owned) return `<span class="pull-status pull-status-owned">${ico('check')}</span>`;
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
          const card = `showIssueModal(${sid}, ${e.number}, {orSeries: true})`;   // the issue's card, as everywhere (2026-10-17)
          return `
            <div class="pull-row" tabindex="0" role="button"
              onclick="${card}" onkeydown="if(event.key==='Enter'||event.key===' ')${card}">
              <div class="pull-thumb-wrap"><img class="pull-thumb" src="/api/series/${sid}/issues/${e.number}/thumbnail" alt=""
                loading="lazy" onerror="this.onerror=function(){this.onerror=null;this.style.opacity='0.15'};this.src='/api/series/${sid}/thumbnail'"></div>
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
  suggested:       ['chip chip-upcoming', 'Suggested'],     // a trade that collects issues nobody shares as singles
  via_trade:       ['chip chip-muted',   'In a trade'],
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
let _activityWaitVisible = false;
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && _activityWaitVisible) { _activityWaitVisible = false; _refreshActivity(); }
});

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
  _paintBadge(queue.filter(q => q.state === 'failed' && fresh(q)).length,
              queue.filter(q => q.state === 'not_found' && fresh(q)).length,
              queue.filter(q => q.state === 'done' && fresh(q)).length);
}

function _paintBadge(failed, warn, done) {
  const el = document.getElementById('activity-badge');
  if (!el) return;
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

// The badge asks for its three numbers, not the whole queue; it rests while the
// tab is hidden and while Activity (which keeps it current itself) is open.
async function _updateActivityBadge() {
  if (document.hidden || currentView === 'activity') return;
  try {
    const c = await api.get(`/api/queue/summary?since=${encodeURIComponent(_actSeen())}`);
    _paintBadge(c.failed, c.not_found, c.done);
  } catch {}
}

function _startBadgePolling() {
  clearInterval(_badgeTimer);
  _updateActivityBadge();
  _badgeTimer = setInterval(_updateActivityBadge, 25000);
  clearInterval(_attentionTimer);
  _updateSettingsBadge();
  _attentionTimer = setInterval(_updateSettingsBadge, 120000);
}

// Accounts (kometa/auth.py /api/users): who can sign in, and on what. Readers read in
// YondeB or an OPDS app; the web app is the admin's alone. A new reader or a new
// password gets a generated one, shown here ONCE to send on — the server keeps a hash.
// The admin's own row has no new-password / sign-out-everywhere: either would sign out
// this very browser mid-click. That's the CLI's job (INSTRUCTIONS, Accounts and login).
const _ACCT_WORDS = ['comet', 'panel', 'inker', 'gutter', 'splash', 'letter', 'cover', 'spine', 'issue', 'arc', 'bleed', 'tone'];
function _acctPassword() {
  const r = crypto.getRandomValues(new Uint32Array(4));
  return [0, 1, 2].map(i => _ACCT_WORDS[r[i] % _ACCT_WORDS.length]).join('-') + '-' + (10 + r[3] % 90);
}
function _acctWhen(s) {
  if (!s) return 'never';
  const d = new Date(s.replace(' ', 'T') + 'Z');
  return isNaN(d) ? s : d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
let _acctNote = null;          // {id, text}: the one-time password line under that account
// What they're reading, from /api/users → reading (auth.reading_summary): the last book
// touched, page of pages, when; then the tallies and whether a phone takes push.
function _acctReading(r) {
  if (!r) return '';
  const l = r.last;
  const where = l ? `${esc(l.series || '')}${l.number != null ? ' #' + esc(String(l.number).replace(/\.0$/, '')) : ''} · ${l.finished ? 'finished' : `page ${l.page}${l.pages ? '/' + l.pages : ''}`} · ${esc(_acctWhen(l.at))}` : 'nothing read yet';
  const tally = [r.in_progress ? `${r.in_progress} in progress` : '', r.finished ? `${r.finished} finished` : '',
                 r.push && r.push.length ? `push: ${r.push.map(esc).join(', ')}` : 'no push'].filter(Boolean).join(' · ');
  return `<div class="acct-reading"><span class="acct-reading-last">${where}</span><span class="acct-meta">${tally}</span></div>`;
}
async function _renderAccounts() {
  const el = document.getElementById('accounts');
  if (!el) return;
  let users;
  try { users = await api.get('/api/users'); } catch { el.innerHTML = '<div class="settings-help bad">Couldn’t load accounts</div>'; return; }
  el.innerHTML = users.map(u => {
    const toks = u.tokens || [];
    const last = toks.map(t => t.last_used).filter(Boolean).sort().pop();
    const admin = u.role === 'admin';
    return `<div class="acct-row${u.disabled ? ' acct-off' : ''}">
      <div class="acct-head">
        <span class="acct-name">${esc(u.username)}</span>
        <span class="settings-opt">${admin ? 'admin' : 'reader'}${u.disabled ? ' · off' : ''}</span>
        <span class="acct-meta">${toks.length} device${toks.length === 1 ? '' : 's'} · last used ${esc(_acctWhen(last))}</span>
      </div>
      ${_acctReading(u.reading)}
      ${toks.length ? `<div class="acct-devices">${toks.map(t => `<div class="acct-device">
        <span>${esc(t.name || 'unnamed')} · ${esc(_acctWhen(t.last_used))}</span>
        <button class="btn-link" onclick="_acctDo('device', '${u.id}', ${t.id}, this)">sign out</button></div>`).join('')}</div>` : ''}
      <div class="acct-actions">
        ${admin ? '' : `<button class="btn-link" onclick="_acctDo('password', '${u.id}', null, this)">new password</button>
        ${toks.length ? `<button class="btn-link" onclick="_acctDo('signout', '${u.id}', null, this)">sign out everywhere</button>` : ''}
        <button class="btn-link" onclick="_acctDo('${u.disabled ? 'on' : 'off'}', '${u.id}', null, this)">${u.disabled ? 'turn on' : 'turn off'}</button>
        <button class="btn-link acct-remove" onclick="_acctDo('remove', '${u.id}', null, this)">remove</button>`}
      </div>
      ${_acctNote && _acctNote.id === u.id ? `<div class="settings-help ok">${esc(_acctNote.label)}: <b class="acct-pw">${esc(_acctNote.pw)}</b> — ${esc(_acctNote.tail)}</div>` : ''}
    </div>`;
  }).join('') + `
    <div class="acct-add">
      <input class="settings-input" id="acct-new" placeholder="new reader’s username" autocomplete="off" autocapitalize="none" spellcheck="false"
        onkeydown="if (event.key === 'Enter') _acctAdd()">
      <button class="btn-link" onclick="_acctAdd()">add reader</button>
    </div>
    <div class="settings-help" id="acct-msg"></div>`;
}
async function _acctAdd() {
  const inp = document.getElementById('acct-new');
  const name = (inp?.value || '').trim();
  if (!name) return;
  const pw = _acctPassword();
  try {
    const u = await api.post('/api/users', { username: name, password: pw, role: 'reader' });
    _acctNote = { id: u.id, label: 'Their password', pw, tail: 'send it with the server address; it isn’t shown again.' };
    _renderAccounts();
  } catch (e) { _acctMsg(e.message === '400' ? 'That username is taken' : 'Couldn’t add that reader', true); }
}
function _acctMsg(t, bad = false) {
  const m = document.getElementById('acct-msg');
  if (m) { m.textContent = t; m.className = 'settings-help' + (bad ? ' bad' : ''); }
}
// remove: tap twice (no browser confirm dialogs in this app) — the first tap arms it for 3 s
async function _acctDo(what, id, tokenId, btn) {
  try {
    if (what === 'remove') {
      if (btn.dataset.armed !== '1') {
        btn.dataset.armed = '1'; btn.textContent = 'remove — sure?';
        setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ''; btn.textContent = 'remove'; } }, 3000);
        return;
      }
      await api.del(`/api/users/${id}`);
    } else if (what === 'password') {
      const pw = _acctPassword();
      await api.patch(`/api/users/${id}`, { password: pw });
      _acctNote = { id, label: 'New password', pw, tail: 'every device of theirs is signed out; it isn’t shown again.' };
    } else if (what === 'signout') {
      const users = await api.get('/api/users');
      for (const t of (users.find(u => u.id === id)?.tokens || [])) await api.del(`/api/users/${id}/tokens/${t.id}`);
    } else if (what === 'device') {
      await api.del(`/api/users/${id}/tokens/${tokenId}`);
    } else if (what === 'off' || what === 'on') {
      await api.patch(`/api/users/${id}`, { disabled: what === 'off' });
    }
    _renderAccounts();
  } catch (e) { _acctMsg(e.message === '409' ? 'That would leave no admin' : 'That didn’t work', true); }
}

// Background sources that need nothing from you: named once so you know they exist,
// 'resting' when one has backed off. (Their numbers board left Activity, 2026-10-17.)
// The Harvesters section's one line: who runs quietly, and who is resting after a
// refusal. Their trouble, when there is any, is written out beneath it (and can be
// dismissed when it isn't yours to fix).
function _harvestersLine(cfg) {
  const names = { prh: 'Penguin Random House', openlibrary: 'Open Library', cbr: 'ComicBookRoundup', fandom: 'Fandom wikis' };
  const b = cfg.extra_budgets || {};
  const resting = Object.keys(names).filter(k => b[k] && b[k].paused).map(k => names[k]);
  return `${Object.values(names).join(' · ')} — no login, they just run.${resting.length ? ` Resting after a refusal, back on their own: ${esc(resting.join(', '))}.` : ''}`;
}
async function _attnDismiss(card, level, btn) {
  btn.disabled = true;
  try { await api.post('/api/settings/attention/dismiss', { card, level }); } catch { btn.disabled = false; return; }
  _updateSettingsBadge();
}
// Every block on Activity wears the same header as a Settings card: bold caps
// title, an italic tag, a rule beneath.
function _actHeader(title, tag = '') {
  return `<div class="settings-card-header"><span>${esc(title)}${tag ? ` <span class="settings-opt">${esc(tag)}</span>` : ''}</span></div>`;
}
// Settings badge: anything in Settings waiting on you (main.settings_attention) —
// a red count on the nav, and the card's own dot goes red with the reason on hover.
let _attentionTimer = null;
async function _updateSettingsBadge() {
  if (document.hidden) return;
  let items;
  try { items = await api.get('/api/settings/attention'); } catch { return; }
  // only what YOU can fix counts; 'wait' (their side) is amber on its own card
  const mine = items.filter(it => it.level !== 'wait');
  const el = document.getElementById('settings-badge');
  if (el) {
    // red = something you can fix here; amber = only hand-offs (tell Claude) pending
    el.textContent = mine.length || '';
    el.className = 'nav-badge' + (mine.some(it => it.level !== 'broken') ? ' red' : mine.length ? ' amber' : '');
  }
  for (const it of items) {
    const dot = document.getElementById(`dot-${it.card}`);
    if (dot) { dot.className = `settings-dot ${it.level === 'fix' ? 'bad' : 'warn'}`; dot.title = it.reason; }
  }
  _renderSettingsAttention(items);
}

// Settings open: what's wrong and what to do, in words at the top of the page
// and again inside the card itself. A red dot with a hover tooltip told an iPad
// nothing. Tap a row at the top to jump to its card.
const _ATTN_SECTION = { sabnzbd: 'sec-usenet', qbit: 'sec-torrent', root: 'ff-root', harvest: 'sec-harvesters', locg: 'sec-harvesters',
                        prh: 'sec-harvesters', openlibrary: 'sec-harvesters', cbr: 'sec-harvesters', fandom: 'sec-harvesters' };
function _attnTarget(card) {
  const id = _ATTN_SECTION[card];
  const el = (id && document.getElementById(id)) || document.getElementById(`sec-${card}`)
    || document.getElementById(`dot-${card}`);
  return el ? (el.closest('.settings-section') || el.closest('.settings-card')) : null;
}
function _renderSettingsAttention(items) {
  const top = document.getElementById('settings-attn');
  if (!top) return;
  document.querySelectorAll('.settings-attn-note').forEach(n => n.remove());
  const mine = items.filter(it => it.level !== 'wait' && it.group !== 'harvesters');
  top.hidden = !mine.length;
  top.innerHTML = mine.map((it, i) => `
    <button class="settings-attn-row${it.level === 'broken' ? ' broken' : ''}" onclick="_attnJump(${i})">
      <span class="settings-attn-title">${esc(it.title || it.card)}</span>
      <span class="settings-attn-reason">${esc(it.reason)}</span>
      ${it.fix ? `<span class="settings-attn-fix">${esc(it.fix)}</span>` : ''}
    </button>`).join('');
  top._items = mine;
  const errs = document.getElementById('harvesters-errors');
  if (errs) errs.innerHTML = '';
  for (const it of items) {
    const note = document.createElement('div');
    note.className = `settings-attn-note${it.level === 'wait' ? ' wait' : it.level === 'broken' ? ' broken' : ''}`;
    const dismiss = it.dismissable ? `<button class="btn-link settings-attn-dismiss" onclick="_attnDismiss('${esc(it.card)}', '${esc(it.level)}', this)" title="Hide until it changes">dismiss</button>` : '';
    note.innerHTML = `<span class="settings-attn-reason">${it.group === 'harvesters' ? `<b>${esc(it.title)}</b> · ` : ''}${esc(it.reason)}${dismiss}</span>${it.fix ? `<span class="settings-attn-fix">${esc(it.fix)}</span>` : ''}`;
    if (it.group === 'harvesters' && errs) { errs.appendChild(note); continue; }   // their trouble lives in their section
    const host = _attnTarget(it.card);
    if (!host) continue;
    const head = host.querySelector(':scope > .settings-section-head, :scope > .settings-card-header');
    head ? head.after(note) : host.prepend(note);
  }
}
function _attnJump(i) {
  const it = document.getElementById('settings-attn')?._items?.[i];
  const host = it && _attnTarget(it.card);
  if (!host) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  host.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) _updateActivityBadge(); });

function _fmtBytes(n) {
  if (!n) return '';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

let _activitySig = null;
let _activityRemoving = false;   // true while a row/card is animating out

// The queue as this page knows it, kept by merging /api/queue?since= answers: a poll
// used to fetch every row, history and all, every 2 s while anything moved. The
// answer carries what changed, every live row, and [id, state] for the lot.
let _actRows = null;             // id → row; null = next poll fetches the whole queue
let _actCursor = '';
let _actFullAt = 0;
async function _fetchQueue() {
  // the whole queue on entry and once a minute: whatever a merge could miss, it can't keep
  const full = !_actRows || Date.now() - _actFullAt > 60000;
  const r = await api.get(`/api/queue?since=${encodeURIComponent(full ? '' : _actCursor)}`);
  const states = new Map(r.states);
  if (full) { _actRows = new Map(); _actFullAt = Date.now(); }
  r.rows.forEach(q => _actRows.set(q.id, q));
  for (const id of [..._actRows.keys()]) if (!states.has(id)) _actRows.delete(id);
  // a row whose state moved without its time (a trade covering a stuck single), or one
  // this page has never seen: start over rather than show it wrong
  if (!full && (r.states.some(([id, st]) => !_actRows.has(id) || _actRows.get(id).state !== st))) {
    _actRows = null;
    return _fetchQueue();
  }
  _actCursor = r.at;
  return [..._actRows.values()].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '') || b.id - a.id);
}

// States that live in the "In Progress" section; everything else is history.
const ACT_ACTIVE_STATES = ['queued','searching','found','downloading','pending_usenet','pending_torrent','processing','proposed','suggested'];

async function renderActivity() {
  clearTimeout(_activityPollTimer);
  _activitySig = null;          // force a full rebuild when entering the view
  _actRows = null;              // and fetch the whole queue
  _actVisitMark = _actSeen();   // what had been seen BEFORE this visit
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Activity';
  // one action (a sweep also starts the queue, so Start Queue went); the view is the Library's
  // chips, In progress / History; Clear history sits at the end of History (2026-10-17)
  document.getElementById('topbar-actions').innerHTML = `
    <button class="btn btn-ghost btn-sm" onclick="triggerSweep(this)">Sweep Missing</button>`;
  document.getElementById('topbar-sub').innerHTML = `<nav class="chip-strip" aria-label="Activity view">
    <button class="browse-filter-tab u-label${_actShowHistory ? '' : ' active'}" id="act-view-now" onclick="_setActHistory(false)">In progress</button>
    <button class="browse-filter-tab u-label${_actShowHistory ? ' active' : ''}" id="act-history-btn" onclick="_setActHistory(true)">History</button></nav>
    <div id="act-jobs"></div>`;
  setApp('<div class="state-msg">Loading...</div>');
  await _refreshActivity();
}

let _actShowHistory = false;
let _actVisitMark = '';       // the seen mark at the moment this visit began
function _setActHistory(on) {
  if (_actShowHistory === on) return;
  _actShowHistory = on;
  _activitySig = null;
  document.getElementById('act-view-now')?.classList.toggle('active', !on);
  document.getElementById('act-history-btn')?.classList.toggle('active', on);
  _refreshActivity();
}

async function _refreshActivity() {
  if (currentView !== 'activity') return;
  // a hidden tab doesn't poll; coming back picks up where it left off
  if (document.hidden) { _activityWaitVisible = true; return; }
  // A removal animation is mid-flight — don't rebuild the list out from under it
  // (a full rebuild here is the flash we're trying to kill). The remover re-polls
  // when it's finished collapsing.
  if (_activityRemoving) return;
  const [all, jobs] = await Promise.all([_fetchQueue(), api.get('/api/listget/jobs').catch(() => [])]);
  _paintListJobs(jobs);
  _ackActivity(all);   // you're looking at it — acknowledge + clear the badge
  // A done row you've already seen on an EARLIER visit is history: it shows
  // once, on the visit after it landed, then gets out of the way. Failed and
  // not-found stay until you deal with them. The mark is fixed for the visit
  // so rows don't vanish mid-look while the page polls.
  const queue = _actShowHistory ? all : all.filter(q => q.state !== 'done' || (q.updated_at || '') > _actVisitMark);
  const hidden = all.length - queue.length;
  const hb = document.getElementById('act-history-btn');
  if (hb) hb.textContent = `History${hidden ? ' (' + hidden + ')' : ''}`;
  // Only the progress %/bytes change between polls; the items + their states usually
  // don't. When they DO change, we reconcile row-by-row IN PLACE — never a full
  // innerHTML rebuild (that recreated every <img> and replayed the cover-in
  // animation on 20 innocent rows because one row moved). See _reconcileActivity.
  // Collapse 2+ active issues of one series into a single synthetic group row
  // BEFORE anything renders. Everything below keys off this display list, not the
  // raw queue, so the reconciler, the sig and the per-tick progress patch all agree.
  const display = _actGroupActive(queue);
  // A group row's fingerprint (range + state-mix) rides in the sig so a membership
  // shift forces a reconcile; bare byte-progress stays in the quiet fast path below.
  const sig = display.map(q => `${q.id}:${q._group ? q._fp : q.state}`).join('|') + (_actShowHistory ? '|h' : '');
  if (sig === _activitySig) {
    display.forEach(q => {
      const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
      const fill = document.getElementById(`actfill-${q.id}`);
      const text = document.getElementById(`acttext-${q.id}`);
      if (fill) fill.style.transform = `scaleX(${pct / 100})`;
      // The group row carries its own aggregate byte line; a single row defers to
      // _actProgressDetail (usenet/torrent/bytes).
      if (text) text.textContent = q._group
        ? (q.progress ? `${pct}% — ${_fmtBytes(q.progress.done)} / ${_fmtBytes(q.progress.total)}` : `${pct}%`)
        : `${pct}%${_actProgressDetail(q)}`;
      // The engine name flips mid-search (GetComics → Usenet → torrent); a bare
      // textContent swap reads as a flash, so crossfade it instead.
      const ss = document.getElementById(`actsearch-${q.id}`);
      if (ss && q.search_status) _softText(ss, q.search_status);
    });
  } else {
    _activitySig = sig;
    await _reconcileActivity(display);
  }
  const hasActive = queue.some(q => ['searching','found','downloading','processing','pending_usenet','pending_torrent'].includes(q.state));
  // Within the post-retry window, keep polling while anything's still queued so
  // we don't freeze on the stale `queued` card and miss the real outcome.
  const pumping = Date.now() < _activityPumpUntil && queue.some(q => q.state === 'queued');
  _paintActClear();
  if (hasActive || pumping || jobs.length) _activityPollTimer = setTimeout(_refreshActivity, 2000);
}

// A reading list's Get missing, still going: it says so here, under the title —
// the pack hunt can run for minutes before a single issue is queued, and a
// click with nothing to show for it reads as a click that did nothing.
function _paintListJobs(jobs) {
  const sub = document.getElementById('act-jobs');        // its own slot: the view chips share the line
  if (!sub) return;
  if (!jobs.length) { sub.innerHTML = ''; return; }
  sub.innerHTML = jobs.map(j => {
    const what = j.phase === 'packs' || j.phase === 'starting' ? 'Looking for packs…'
      : `Searching entries · ${j.done} of ${j.total}${j.queued ? ` · ${j.queued} queued` : ''}`;
    return `<a class="act-job" onclick="navigate('readlist', {id: ${j.list_id}})"><span class="act-job-spin" aria-hidden="true"></span>
      <span class="act-job-name">${esc(j.name || 'Reading list')}</span><span class="act-job-what">Get missing · ${what}</span></a>`;
  }).join('');
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
    ? ' · Usenet' + (q.search_status ? ' · ' + q.search_status : '')
    : q.state === 'pending_torrent'
    ? ' · Torrent' + (q.search_status ? ' · ' + q.search_status : '')
    : (q.progress ? ' — ' + _fmtBytes(q.progress.done) + ' / ' + _fmtBytes(q.progress.total) : '');
}

// Activity rows are kind-agnostic: a trade shows "Vol N" and the series cover
// (no per-issue thumbnail), an issue shows "#N" and its own.
// A reading list's pack is one torrent/NZB for a whole list, filed under one of
// the runs it fills (the queue keys on a series) with a placeholder issue number
// (-1000 - list*10 - n). Shown as itself: 'Ghost Racers #-1260' was the Secret Wars pack.
function _actPack(q) {
  if (q.kind !== 'list_pack') return null;
  let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
  return { title: (m.pack_title || 'Reading-list pack').replace(/\s+/g, ' ').trim(), fills: (m.fills || []).length, listId: m.list_id };
}
function _actLabel(q) {
  const pack = _actPack(q);
  if (pack) return `Reading-list pack${pack.fills ? ` · fills ${pack.fills}` : ''}`;
  if (q.issue_number === -1 && (q.kind || 'issue') === 'issue') return 'Complete run';   // the whole-run pack sentinel, not 'issue #-1'
  if (q.kind === 'trade') {
    if (String(q.locg_id) === '-1') return 'Complete run';     // a whole-run pack of singles, not a collected edition
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return _volLabel(m, 'Trade');
  }
  return `#${fmtNum(q.issue_number)}`;
}
// Activity asks for covers with ?fast=1: a cover not made yet answers 204 at once
// (the server keeps making it) instead of parking a browser connection for seconds
// on the comics share. The row dims, then asks once more when it's likely done.
function _actCoverMiss(img, fallback) {
  if (fallback && !img.dataset.fellBack) { img.dataset.fellBack = '1'; img.src = fallback; return; }
  img.style.opacity = '0.15';
  if (img.dataset.retried) { img.onerror = null; return; }
  img.dataset.retried = '1';
  const src = img.src;
  setTimeout(() => { if (img.isConnected) { img.style.opacity = ''; img.src = src + (src.includes('?') ? '&' : '?') + 'r=1'; } }, 8000);
}
function _actThumb(q) {
  if (!q.tracked_series_id) return '';
  const seriesThumb = `/api/series/${q.tracked_series_id}/thumbnail?fast=1`;
  const img = (src, fallback) => `<img src="${esc(src)}" alt="" loading="lazy" onerror="_actCoverMiss(this, ${fallback ? `'${fallback}'` : 'null'})">`;
  if (q.kind === 'trade') {
    // A proposal shows the cover of what actually came back; otherwise the
    // trade's own LOCG cover (stashed in meta), falling back to the series.
    if (q.state === 'proposed') return img(`/api/queue/${q.id}/proposal-cover`, seriesThumb);
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return m.cover ? img(m.cover, seriesThumb) : img(seriesThumb, null);
  }
  if (q.kind === 'list_pack' || q.issue_number == null || q.issue_number < 0) return img(seriesThumb, null);
  return img(`/api/series/${q.tracked_series_id}/issues/${q.issue_number}/thumbnail?fast=1`, seriesThumb);
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
          <div class="act-progress-track"><div class="act-progress-fill" id="actfill-${q.id}" style="transform:scaleX(${pct / 100})"></div></div>
          <div class="act-progress-text" id="acttext-${q.id}">${pct}%${esc(_actProgressDetail(q))}</div>
        </div>`;
  }
  if (q.state === 'suggested') {
    let m = {}; try { m = JSON.parse(q.meta_json || '{}'); } catch {}
    return `<div class="act-row-reason" style="color:var(--amb)">Fills ${esc(m.fills_label || '')} — they're in <strong>${esc(m.edition_title || 'this trade')}</strong>${m.format ? ' (' + esc(m.format) + (m.collects ? `, ${m.collects} issues` : '') + ')' : ''}. Nothing downloads until you say.</div>`;
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
  if (q.state === 'suggested') {
    btns = `
            <button class="btn btn-ghost btn-sm" onclick="tradeDecide(${q.id}, 'not-this-trade', this)" title="Not this edition — offer the next best, or nothing">Not this one</button>
            <button class="btn btn-primary btn-sm" onclick="tradeDecide(${q.id}, 'get-trade', this)" title="Download this trade">Get this trade</button>`;
    return `${_actChip(q.state)}${btns}`;
  }
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
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from queue" aria-label="Remove from queue">${ico('x')}</button>`;
  } else if (!ACT_ACTIVE_STATES.includes(q.state)) {
    const retry = q.state !== 'done' ? `<button class="btn btn-ghost btn-sm" onclick="retryQueue(${q.id}, this)">Retry</button>` : '';
    btns = `
            ${retry}
            <button class="btn btn-ghost btn-sm" onclick="removeQueue(${q.id}, this)" title="Remove from history" aria-label="Remove from history">${ico('x')}</button>`;
  }
  // You know it's on GetComics and usenet/torrent are sulking: jump the cascade.
  const single = q.kind !== 'trade' && q.issue_number != null && q.issue_number !== -1;
  if (single && ['pending_usenet', 'pending_torrent', 'downloading', 'not_found', 'failed'].includes(q.state)) {
    btns = `<button class="btn btn-ghost btn-sm" onclick="tryGetComics(${q.id}, this)" title="Cancel the usenet/torrent job and fetch it from GetComics now">GetComics</button>` + btns;
  }
  return `${_actChip(q.state)}${btns}`;
}

async function tryGetComics(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Asking…';
  try {
    await api.post(`/api/queue/${id}/try-getcomics`, {});
    showToast('Cancelled the download — trying GetComics');
  } catch (e) {
    btn.disabled = false; btn.textContent = 'GetComics';
    showToast('Couldn’t switch to GetComics — ' + (e?.message || e), 'error');
    return;
  }
  _activityPumpUntil = Date.now() + 30000;
  _activitySig = null;
  _refreshActivity();
}

async function tradeDecide(qid, action, btn) {
  btn.disabled = true;
  try {
    await api.post(`/api/queue/${qid}/${action}`, {});
    showToast(action === 'get-trade' ? 'Getting the trade' : 'Dropped — the next pass offers the next best, if there is one');
    _activityPumpUntil = Date.now() + 30000;
    renderActivity();
  } catch (e) { btn.disabled = false; showToast('Couldn’t do that', 'error'); }
}

async function proposalDecide(qid, action, btn) {
  btn.disabled = true;
  try {
    const r = await api.post(`/api/queue/${qid}/${action}`, {});
    showToast(action === 'confirm' ? `Placed${r.placed ? ` — ${r.placed.length} file${r.placed.length === 1 ? '' : 's'}` : ''}` : 'Rejected — the next search tries something else');
    renderActivity();
  } catch (e) { btn.disabled = false; showToast(`Couldn’t ${action}`, 'error'); }
}

// --- Same-series grouping -------------------------------------------------
// When 2+ single issues of ONE series are active at once, we don't want N near-
// identical rows screaming the same cover at you — collapse them into one row that
// shows the series, the issue range and one aggregate bar. Trades, reading-list
// packs, run sentinels (issue_number < 0) and anything untracked are NEVER grouped;
// they keep their own row. A group that shrinks to a single member stops being a
// group and the real row comes back — the reconciler handles the swap.
function _actGroupable(q) {
  return ACT_ACTIVE_STATES.includes(q.state)
    && q.tracked_series_id
    && q.kind !== 'trade' && q.kind !== 'list_pack'
    && q.issue_number != null && q.issue_number >= 0;
}

// Issue numbers → a tight label. Contiguous integer run reads as "#1–5"; anything
// gappy or fractional falls back to the explicit list "#1, #3, #5".
function _actIssueRange(nums) {
  const u = [...new Set(nums)].sort((a, b) => a - b);
  if (!u.length) return '';
  const allInt = u.every(n => Number.isInteger(n));
  if (allInt && u.length > 1 && u[u.length - 1] - u[0] + 1 === u.length) {
    return `#${fmtNum(u[0])}–${fmtNum(u[u.length - 1])}`;
  }
  return u.map(n => `#${fmtNum(n)}`).join(', ');
}

// States collapsed to plain words for the group's "1 downloading · 4 filing" line.
const _ACT_GROUP_WORDS = {
  downloading: 'downloading', pending_usenet: 'downloading', pending_torrent: 'downloading',
  processing: 'filing', searching: 'searching', found: 'searching',
  queued: 'queued', proposed: 'proposed', suggested: 'suggested',
};
// Pick one representative state for the chip/thumb — the busiest wins.
const _ACT_GROUP_PRIO = ['downloading', 'pending_usenet', 'pending_torrent', 'processing', 'searching', 'found', 'proposed', 'suggested', 'queued'];

// Fold a series' active members into one synthetic queue item. The id is stable
// (`grp-<seriesId>`) so the reconciler can morph it in place across polls, and _fp
// captures everything that should trigger a re-render EXCEPT raw bytes (those ride
// the quiet per-tick path, same as a single row).
function _actMakeGroup(members) {
  const sid = members[0].tracked_series_id;
  let done = 0, total = 0, hasProg = false;
  for (const m of members) {
    if (m.progress && m.progress.total) { done += m.progress.done || 0; total += m.progress.total; hasProg = true; }
  }
  let rep = members[0].state;
  for (const s of _ACT_GROUP_PRIO) { if (members.some(m => m.state === s)) { rep = s; break; } }
  const nums = members.map(m => m.issue_number).filter(n => n != null);
  const range = _actIssueRange(nums);
  const counts = new Map();
  for (const m of members) { const w = _ACT_GROUP_WORDS[m.state] || m.state; counts.set(w, (counts.get(w) || 0) + 1); }
  const mix = Array.from(counts, ([w, n]) => `${n} ${w}`).join(' · ');
  return {
    id: `grp-${sid}`,
    _group: true,
    _members: members,
    _range: range,
    _mix: mix,
    _hasProg: hasProg,
    _fp: `${rep}|${[...new Set(nums)].sort((a, b) => a - b).join(',')}|${mix}|${hasProg ? 1 : 0}`,
    tracked_series_id: sid,
    title: members[0].title,
    publisher: members[0].publisher,
    kind: 'issue',
    issue_number: -2,            // < 0 → _actThumb serves the series cover, not an issue
    state: rep,                  // a real active state so it lands in the In Progress section
    progress: hasProg ? { done, total } : null,
  };
}

// Pre-process the raw queue into the display list: each 2+ same-series active group
// becomes ONE synthetic row (emitted at its first member's position), everything
// else passes straight through. Preserves order; returns the queue unchanged (same
// reference) when there's nothing to collapse, so the no-group path is a cheap no-op.
function _actGroupActive(queue) {
  const bySeries = new Map();
  for (const q of queue) {
    if (!_actGroupable(q)) continue;
    const arr = bySeries.get(q.tracked_series_id) || [];
    arr.push(q);
    bySeries.set(q.tracked_series_id, arr);
  }
  const collapse = new Set();
  for (const [sid, arr] of bySeries) { if (arr.length >= 2) collapse.add(sid); }
  if (!collapse.size) return queue;
  const out = [], emitted = new Set();
  for (const q of queue) {
    if (_actGroupable(q) && collapse.has(q.tracked_series_id)) {
      if (emitted.has(q.tracked_series_id)) continue;
      emitted.add(q.tracked_series_id);
      out.push(_actMakeGroup(bySeries.get(q.tracked_series_id)));
    } else {
      out.push(q);
    }
  }
  return out;
}

// Inner HTML of a group row's .act-row-meta: title, "publisher · #range · N issues",
// an aggregate bar (only when a member is actually transferring bytes) and the
// plain-words state-mix line. Shared by the initial render and the morph path.
function _actGroupMeta(q) {
  const title = `<div class="act-row-title u-truncate">${esc(q.title)}</div>`;
  const issue = `<div class="act-row-issue">${q.publisher ? esc(q.publisher) + ' · ' : ''}${esc(q._range)} · ${q._members.length} issues</div>`;
  let sub = '';
  if (q._hasProg) {
    const pct = q.progress && q.progress.total ? Math.round(q.progress.done / q.progress.total * 100) : 0;
    sub += `
        <div class="act-row-progress">
          <div class="act-progress-track"><div class="act-progress-fill" id="actfill-${q.id}" style="transform:scaleX(${pct / 100})"></div></div>
          <div class="act-progress-text" id="acttext-${q.id}">${pct}% — ${esc(_fmtBytes(q.progress.done))} / ${esc(_fmtBytes(q.progress.total))}</div>
        </div>`;
  }
  sub += `<div class="act-row-reason">${esc(q._mix)}</div>`;
  return title + issue + sub;
}

function _actGroupRowHtml(q) {
  const nav = ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"`;
  return `
        <div class="act-row" data-qid="${q.id}" data-qstate="${esc(q._fp)}" data-qgroup="1">
          <div class="act-row-cover">${_actThumb(q)}</div>
          <div class="act-row-meta"${nav}>${_actGroupMeta(q)}</div>
          <div class="act-row-actions">${_actChip(q.state)}</div>
        </div>`;
}

function _actRowHtml(q) {
  if (q._group) return _actGroupRowHtml(q);
  const active = ACT_ACTIVE_STATES.includes(q.state);
  const errTip = q.error ? ` title="${esc(q.error)}"` : '';
  const pack = _actPack(q);
  // a row is the run it's for (the owner, 2026-10-17: not the issue's card); a pack its list
  const nav = pack && pack.listId ? ` style="cursor:pointer" onclick="navigate('readlist',{id:${pack.listId}})"`
    : q.tracked_series_id ? ` style="cursor:pointer" onclick="navigate('series-detail',{id:${q.tracked_series_id}})"` : '';
  return `
        <div class="act-row${q.state === 'done' ? ' done' : ''}" data-qid="${q.id}" data-qstate="${q.state}"${errTip}>
          <div class="act-row-cover">${_actThumb(q)}</div>
          <div class="act-row-meta"${nav}>
            <div class="act-row-title u-truncate">${esc(pack ? pack.title : q.title)}</div>
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
    setApp(`<div class="act-wrap">
      <div class="act-section settings-card" data-sec="empty">
        ${_actHeader('Downloads')}
        <div class="act-empty-line">${_actShowHistory ? 'Nothing in the queue' : 'Nothing active'}</div>
      </div></div>`);
    return;
  }

  let html = '<div class="act-wrap">';
  if (inProgress.length) {
    html += `<div class="act-section settings-card" data-sec="progress">
      ${_actHeader('Downloads')}
      ${inProgress.map(_actRowHtml).join('')}
    </div>`;
  }
  if (completed.length) {
    html += `<div class="act-section settings-card" data-sec="completed">
      ${_actHeader(_actShowHistory ? 'History' : 'Completed')}
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
  // A group row changes shape when its membership/range/state-mix shifts (its _fp).
  // Rebuild the meta block only — the cover <img> lives in a sibling, so it survives.
  if (q._group) {
    el.dataset.qstate = q._fp;
    const meta = el.querySelector('.act-row-meta');
    if (meta) meta.innerHTML = _actGroupMeta(q);
    const actions = el.querySelector('.act-row-actions');
    if (actions) actions.innerHTML = _actChip(q.state);
    return;
  }
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
          ? `<div class="act-section settings-card act-fade-in" data-sec="progress">${_actHeader('Downloads')}</div>`
          : `<div class="act-section settings-card act-fade-in" data-sec="completed">${_actHeader(_actShowHistory ? 'History' : 'Completed')}</div>`;
        secEl = t.content.firstElementChild;
        sec === 'progress' ? wrap.prepend(secEl) : wrap.append(secEl);
      }
      const hdr = secEl.querySelector('.settings-card-header');
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
          if (el.dataset.qstate !== (q._fp || q.state)) _morphActRow(el, q);
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
    wrap.querySelectorAll('.act-section:not([data-sec="harvest"])').forEach(sec => {
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
  btn.innerHTML = 'Queued ' + ico('check');
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Sweep Missing'; renderActivity(); }, 1500);
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

// The end of the History view: Clear history, when there's history to clear.
function _paintActClear() {
  const wrap = document.querySelector('#app .act-wrap');
  const has = _actShowHistory && wrap && wrap.querySelector('.act-section[data-sec="completed"] .act-row');
  let el = document.getElementById('act-clear');
  if (!has) { el?.remove(); return; }
  if (!el) {
    wrap.insertAdjacentHTML('beforeend', `<div class="act-clear" id="act-clear">
      <button class="btn btn-ghost btn-sm" onclick="clearHistory(this)">Clear history</button></div>`);
  } else if (el !== wrap.lastElementChild) wrap.append(el);
}

async function clearHistory(btn) {
  // tap twice (no browser confirm dialogs in this app): the first tap arms it for 3 s
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1'; btn.textContent = 'Clear history — sure?';
    setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ''; btn.textContent = 'Clear history'; } }, 3000);
    return;
  }
  btn.disabled = true; btn.textContent = 'Clearing…';
  try {
    await api.post('/api/queue/clear-history', {});
  } catch (e) {
    btn.disabled = false; btn.dataset.armed = ''; btn.textContent = 'Clear history';
    showToast('Clear failed'); console.error(e); return;
  }
  // Fade the completed rows out in place — don't rebuild the whole list (the in-progress
  // section would flash). Stagger them a touch for a tidy cascade, then drop the sig so
  // the next poll reconciles cleanly.
  _activityRemoving = true;
  // only what the server cleared — finished and not-found; a failed row stays (it used to
  // fade with the rest and pop back on the next poll)
  const rows = Array.from(document.querySelectorAll(
    '.act-section[data-sec="completed"] .act-row:is([data-qstate="done"], [data-qstate="not_found"])'));
  rows.forEach((row, i) => { row.style.transitionDelay = `${i * 30}ms`; });
  await Promise.all(rows.map(_animateRowOut));
  // The Completed section header is now stranded with no rows — yank it too.
  document.querySelectorAll('.act-section').forEach(sec => {
    if (!sec.querySelector('.act-row')) sec.remove();
  });
  _activitySig = null;
  _activityRemoving = false;
  document.getElementById('act-clear')?.remove();
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

  setApp(`
    <div class="settings-attn" id="settings-attn" hidden></div>
    <div class="settings-grid">
      <div>
        <div class="settings-card">
          ${_settingsHeader('Comics Library', 'required', 'library')}
          <div class="settings-field">
            <label class="settings-field-label u-label" for="ff-root">Library path</label>
            ${cfg.comics_root_ok
              ? `<div class="settings-help" id="ff-root">${esc(cfg.comics_root || '')} <span style="color:var(--tq)">· the container's mount, set in compose</span></div>`
              : folderField('root', cfg.comics_root, 'fs', async (path) => {
              const c = await api.patch('/api/config', { comics_root: path });
              _appConfig = { ..._appConfig, comics_root: path, comics_root_ok: c.comics_root_ok };
              _updateRootStatus(c.comics_root_ok);
            })}
            <div class="settings-help" id="root-status"></div>
          </div>
          <div class="settings-help" id="backup-status">${_backupLine(cfg)}</div>
        </div>
        <div class="settings-card" style="margin-top:32px">
          ${_settingsHeader('Sync Schedule', '', 'schedule')}
          ${_settingsField('f-sync-hours', 'Hours (24h, comma-separated)', cfg.sync_hours)}
        </div>
        <div class="settings-card" style="margin-top:32px">
          ${_settingsHeader('Accounts', 'who signs in', 'accounts')}
          <div id="accounts"></div>
        </div>
        <div class="settings-card" style="margin-top:32px">
          ${_settingsHeader('FlareSolverr', 'cloudflare', 'flaresolverr', true, cfg.flaresolverr_configured)}
          ${_settingsField('f-flaresolverr-url', 'Server URL', cfg.flaresolverr_url, { ph: 'http://host:8191' })}
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
        <div class="settings-section ${cfg.metron_enabled ? '' : 'section-off'}" id="sec-metron" style="margin-bottom:32px">
          ${_settingsSectionHead('Metron', 'primary catalogue', 't-metron', cfg.metron_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-metron-token', 'API Token', '', { set: cfg.metron_configured, ph: 'metron.cloud › account › API tokens', test: { cardId: 'metron', configured: cfg.metron_configured } })}
            </div>
          </div></div>
        </div>
        <div class="settings-section ${cfg.comicvine_enabled ? '' : 'section-off'}" id="sec-comicvine">
          ${_settingsSectionHead('ComicVine', 'storylines', 't-comicvine', cfg.comicvine_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-cv-apikey', 'API Key', '', { set: cfg.comicvine_configured, ph: 'Enter API key', test: { cardId: 'comicvine', configured: cfg.comicvine_configured } })}
            </div>
          </div></div>
        </div>
        <div class="settings-section ${cfg.gcd_enabled ? '' : 'section-off'}" id="sec-gcd" style="margin-top:32px">
          ${_settingsSectionHead('Grand Comics Database', 'back catalogue', 't-gcd', cfg.gcd_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-gcd-user', 'Username', cfg.gcd_user, { ph: 'Email or username', test: { cardId: 'gcd', configured: cfg.gcd_configured } })}
              ${_settingsField('f-gcd-pass', 'Password', '', { set: cfg.gcd_configured, ph: 'Enter password' })}
            </div>
          </div></div>
        </div>
        <div class="settings-card" id="sec-harvesters" style="margin-top:32px">
          ${_settingsHeader('Harvesters', 'background', 'harvesters')}
          <div class="settings-help">${_harvestersLine(cfg)}</div>
          <div class="settings-row-toggle" style="margin-top:12px"><span>Download Fandom gallery images</span>${_settingsToggle('t-fandom-download', !!cfg.fandom_download, 'Download Fandom gallery images')}</div>
          <div id="harvesters-errors"></div>
        </div>
        <div class="settings-section ${cfg.hardcover_enabled ? '' : 'section-off'}" id="sec-hardcover" style="margin-top:32px">
          ${_settingsSectionHead('Hardcover', 'book ratings', 't-hardcover', cfg.hardcover_enabled)}
          <div class="settings-section-body"><div class="settings-section-inner">
            <div class="settings-card">
              ${_settingsField('f-hardcover-token', 'API Token', '', { set: cfg.hardcover_configured, ph: 'hardcover.app › settings › API', test: { cardId: 'hardcover', configured: cfg.hardcover_configured } })}
              ${_requestsToday(cfg, 'hardcover')}
            </div>
          </div></div>
        </div>
      </div>
    </div>
  `);
  _updateRootStatus(cfg.comics_root_ok);
  _renderAccounts();
  _updateSettingsBadge();
  // Sections that load already-off start collapsed WITHOUT animation (no
  // fold-in flash on entering Settings). Prowlarr is the master — off folds the
  // whole usenet+torrent branch it wraps.
  if (!cfg.comicvine_enabled) _collapseSection(document.getElementById('sec-comicvine'), true, false);
  if (!cfg.gcd_enabled)       _collapseSection(document.getElementById('sec-gcd'), true, false);
  for (const k of ['hardcover']) {
    if (!cfg[`${k}_enabled`]) _collapseSection(document.getElementById(`sec-${k}`), true, false);
  }
  if (!cfg.metron_enabled)    _collapseSection(document.getElementById('sec-metron'), true, false);
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
  'f-sync-hours':  { card: 'schedule',  key: 'sync_hours' },
  'f-sab-url':     { card: 'sabnzbd',   key: 'sab_url',          test: 'sabnzbd' },
  'f-sab-apikey':  { card: 'sabnzbd',   key: 'sab_apikey',       test: 'sabnzbd', secret: true },
  'f-qbit-url':        { card: 'qbit',     key: 'qbit_url',         test: 'qbit' },
  'f-qbit-user':       { card: 'qbit',     key: 'qbit_user',        test: 'qbit' },
  'f-qbit-pass':       { card: 'qbit',     key: 'qbit_pass',        test: 'qbit', secret: true },
  'f-prowlarr-url':    { card: 'prowlarr', key: 'prowlarr_url',     test: 'prowlarr' },
  'f-flaresolverr-url': { card: 'flaresolverr', key: 'flaresolverr_url', test: 'flaresolverr' },
  'f-prowlarr-apikey': { card: 'prowlarr', key: 'prowlarr_apikey',  test: 'prowlarr', secret: true },
  'f-cv-apikey':       { card: 'comicvine', key: 'cv_api_key',      test: 'comicvine', secret: true },
  'f-metron-token':    { card: 'metron',    key: 'metron_token',    test: 'metron', secret: true },
  'f-gcd-user':        { card: 'gcd',       key: 'gcd_user',        test: 'gcd' },
  'f-gcd-pass':        { card: 'gcd',       key: 'gcd_pass',        test: 'gcd', secret: true },
  'f-hardcover-token': { card: 'hardcover', key: 'hardcover_token', test: 'hardcover', secret: true },
};

const _TEST_ENDPOINTS = { sabnzbd: 'sab', qbit: 'qbit', prowlarr: 'prowlarr', flaresolverr: 'flaresolverr', comicvine: 'comicvine', gcd: 'gcd', metron: 'metron', hardcover: 'hardcover' };

// Nightly backup of the record (kometa/backup.py): database copy + cover mirror to the NAS.
function _backupLine(cfg) {
  const l = cfg.backup_last || {};
  const when = l.started_at ? new Date(l.started_at).toLocaleString('en', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'never';
  const what = l.ok ? ` · ${(l.db_bytes / 1e6).toFixed(1)} MB database${l.covers_copied ? `, ${l.covers_copied} covers` : ''}` : (l.error ? ` · failed: ${esc(l.error)}` : '');
  const stale = l.started_at && (Date.now() - new Date(l.started_at)) > 30 * 3600 * 1000;
  const flag = !l.ok && l.error ? ' <span style="color:var(--pink)">failed</span>' : stale ? ' <span style="color:var(--amb)">· no run since then</span>' : '';
  return `Backup to the NAS nightly at 3:30 · last ${esc(when)}${what}${flag} <button class="btn-link" onclick="_backupNow(this)">back up now</button>`;
}

async function _backupNow(btn) {
  btn.disabled = true; btn.textContent = 'backing up…';
  try {
    await api.post('/api/backup', {});
    const until = Date.now() + 30 * 60 * 1000;               // a hung backup is not an endless 3 s poll
    const poll = setInterval(async () => {
      if (_tabHidden()) return;
      if (Date.now() > until) { clearInterval(poll); btn.disabled = false; btn.textContent = 'back up now'; return; }
      try {
        const st = await api.get('/api/backup');
        if (st.running) return;
        clearInterval(poll);
        const cfg = await api.get('/api/config');
        const el = document.getElementById('backup-status'); if (el) el.innerHTML = _backupLine(cfg);
      } catch { clearInterval(poll); btn.disabled = false; btn.textContent = 'back up now'; }
    }, 3000);
  } catch { btn.disabled = false; btn.textContent = 'back up now'; }
}

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
  // secret: 'mask' — a token field: a plain TEXT input, shown as typed. Not a
  // password; type=password made Safari invent a strong one and -webkit-text-security
  // dots still had it offering to save (2026-10-14). It never round-trips, so it's on
  // screen only while you enter it. The data-* attrs wave off 1Password/LastPass.
  const mask = f.secret === 'mask';
  return `
    <div class="settings-field">
      ${labelRow}
      <input class="settings-input" id="${id}"
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

// The polite extras (harvest 1i: PRH, Open Library, ComicBookRoundup, Fandom): no
// login, so the card is the toggle plus today's allowance (and any extra rows).
function _requestsToday(cfg, key) {
  const b = (cfg.extra_budgets || {})[key];
  const today = b ? `${Number(b.used).toLocaleString()} of ${Number(b.cap).toLocaleString()}${b.paused ? ' · resting' : ''}` : '—';
  return `<div class="settings-stat"><span>Requests today</span><b>${today}</b></div>`;
}

function _politeSourceSection(key, title, tag, cfg, extra = '') {
  const on = !!cfg[`${key}_enabled`];
  // no login, so no test row to carry the dot: it sits in the head (kometa/health.py
  // lights it when the source keeps failing).
  return `<div class="settings-section ${on ? '' : 'section-off'}" id="sec-${key}" style="margin-top:32px">
    ${_settingsSectionHead(title, tag, `t-${key}`, on, `dot-${key}`)}
    <div class="settings-section-body"><div class="settings-section-inner">
      <div class="settings-card">
        ${_requestsToday(cfg, key)}
        ${extra}
      </div>
    </div></div>
  </div>`;
}

// One header row per toggleable section (sources / Prowlarr / Usenet / Torrent):
// title + role tag + the enable toggle, underlined. Off folds the body; head stays.
function _settingsSectionHead(title, tag, toggleId, on, dotId = '') {
  return `<div class="settings-section-head">
    <span class="settings-section-title">${esc(title)}${tag ? ` <span class="settings-opt">${tag}</span>` : ''}${dotId ? ` <span class="settings-dot" id="${dotId}"></span>` : ''}</span>
    ${_settingsToggle(toggleId, on, `${title} ${on ? 'enabled' : 'disabled'}`)}
  </div>`;
}

// Toggle → persist the flag + fold/unfold the section. Backend reads the flag
// (gates the search cascade for usenet/torrent, each source's accessor for the rest).
const _SOURCE_TOGGLES = {
  't-comicvine': { key: 'comicvine_enabled', section: 'sec-comicvine', label: 'ComicVine' },
  't-metron':    { key: 'metron_enabled',    section: 'sec-metron',    label: 'Metron' },
  't-gcd':       { key: 'gcd_enabled',       section: 'sec-gcd',       label: 'Grand Comics Database' },
  't-fandom':    { key: 'fandom_enabled',    section: 'sec-fandom',    label: 'Fandom wikis' },
  // a flag inside the Fandom card, nothing to fold: downloads stay off until the
  // cover store leaves ginbako's internal disk
  't-fandom-download': { key: 'fandom_download', section: null, label: 'Fandom gallery downloads' },
  't-hardcover': { key: 'hardcover_enabled', section: 'sec-hardcover', label: 'Hardcover' },
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
    _updateSettingsBadge();
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

function _validHourWindow(v) {
  const m = /^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$/.exec(v);
  return !!m && +m[1] <= 23 && +m[2] <= 23;
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
  try {
    const cfg = await api.patch('/api/config', body);
    // Settings autosave changes what the SERVER knows, but every render decision
    // gated on _appConfig (Arcs tab, …) was reading the
    // STALE snapshot from boot() until a hard reload — a toggle could report
    // 'enabled' via toast while the UI it unlocks stayed invisible all session.
    _appConfig = cfg;
    el.dataset.last = val;
    if (f.secret) { el.value = ''; el.placeholder = 'Leave blank to keep current'; }
    _whisper(f.card, 'saved');
    if (f.key === 'comics_root') _updateRootStatus(cfg.comics_root_ok);
    if (f.test) await testIntegration(f.test);       // re-verify after cred change
    _updateSettingsBadge();
  } catch (e) {
    _whisper(f.card, 'save failed', true);
    showToast(`Save failed: ${e.message || f.key}`, 'error');
  }
}

function _updateRootStatus(ok) {
  const el = document.getElementById('root-status');
  if (!el) return;
  // No news is good news — only surface the path when it's a problem.
  el.innerHTML = ok ? '' : ico('x') + ' path missing or read-only';
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
    _updateSettingsBadge();
    renderSettings();
  } catch (e) {
    showToast(`Disconnect failed: ${e.message || integ}`, 'error');
  }
}

// Newznab indexer list retired — Prowlarr aggregates every indexer now, so
// there's nothing to hand-enter. (The Add/Remove UI + endpoints are gone.)

// --- Modal ---

// The fade-out a closeModal() started: a window opened during it (close one card,
// open the next) was hidden by it 200 ms later — "Find in catalogue" opened the Add
// search and it vanished (2026-10-16). Opening cancels it and does its resets.
let _modalCloseTimer = null;

function showModal(html) {
  const modal = document.getElementById('modal');
  if (_modalCloseTimer) {
    clearTimeout(_modalCloseTimer);
    _modalCloseTimer = null;
    modal.classList.remove('modal-wide');
    modal.style.height = '';
    modal.style.transition = '';
  }
  modal.innerHTML = html;
  modal._openedAt = performance.now();
  modal.classList.remove('hidden');
  const backdrop = document.getElementById('modal-backdrop');
  backdrop.classList.remove('hidden', 'closing');
  // opened from inside the reader (its Next issue): the window sits over the reader.
  // Only then — one still fading out as a book opens must stay under it
  backdrop.classList.toggle('over-reader', document.body.classList.contains('reading'));
  const first = modal.querySelector('button, input, [tabindex]');
  if (first) setTimeout(() => first.focus(), 30);
  _wantSync(modal);
}

function closeModal() {
  const modal = document.getElementById('modal');
  const backdrop = document.getElementById('modal-backdrop');
  if (modal.classList.contains('hidden') || backdrop.classList.contains('closing')) return;
  clearTimeout(_issueModalPollTimer);
  document.getElementById('variant-lightbox')?.classList.add('hidden');  // ensure lightbox closes with the modal
  backdrop.classList.add('closing');          // plays scrim-out + modal-out
  _modalCloseTimer = setTimeout(() => {
    _modalCloseTimer = null;
    modal.classList.add('hidden');
    modal.classList.remove('modal-wide');
    // A close mid-height-tween never gets its transitionend — clear the pinned
    // inline height or the NEXT modal opens squeezed to this one's size.
    modal.style.height = '';
    modal.style.transition = '';
    backdrop.classList.add('hidden');
    backdrop.classList.remove('closing', 'over-reader');
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
  // Still on its way in: content that lands now just rides the entrance. Tweening the
  // height on top of the rise grew the modal around its centre while it slid —
  // a stretch, not a fade-up (details got fast enough to land mid-entrance).
  if (performance.now() - (modal._openedAt || 0) < 360) { mutate(); return; }
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

// Community ratings (v1, 2026-10-12 — "I'll dial it in later"): every source on a
// 10-point scale under your own 5 stars, with how many people it rests on. LOCG's
// 0–5 and the book sites' 0–5 double; ComicBookRoundup is already out of 10.
// Tapping the line opens the detail. A source with nothing for this issue isn't shown.
function _communityHtml(c) {
  if (!c) return '';
  const k = n => n == null ? '' : n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  const sc = (src, v, n) => v == null ? '' : `<span class="comm-sc"><span class="comm-src u-label">${esc(src)}</span>`
    + `<span class="comm-v${v >= 7.5 ? ' hi' : v < 6 ? ' lo' : ''}">${v.toFixed(1)}</span>${n ? `<span class="comm-n">${k(n)}</span>` : ''}</span>`;
  const locg = c.rating != null ? c : null, cbr = c.cbr, hc = c.hardcover, ol = c.openlibrary;
  const line = [
    locg && sc('LOCG', locg.rating * 2, locg.ratings),
    cbr && sc('Critics', cbr.critic, cbr.critic_reviews),
    cbr && sc('Readers', cbr.user, cbr.user_reviews),
    hc && hc.rating != null && sc('Hardcover', hc.rating * 2, hc.count),
    ol && ol.rating != null && sc('Open Library', ol.rating * 2, ol.count),
  ].filter(Boolean).join('');
  if (!line) return '';
  const more = [];
  if (locg) {
    const h = locg.histogram || {}, max = Math.max(1, ...Object.values(h));
    const top = Object.entries(h).sort((a, b) => b[1] - a[1])[0]?.[0];
    more.push(`<div>LOCG${locg.verdict ? ' · ' + esc(locg.verdict) : ''} · ${(locg.ratings || 0).toLocaleString()} ratings</div>`);
    if (Object.keys(h).length) more.push(`<div class="comm-hist">${[1,2,3,4,5,6,7,8,9,10].map(i =>
      `<i class="${String(i) === top ? 'top' : ''}" style="height:${Math.max(3, Math.round(100 * (h[i] || 0) / max))}%"></i>`).join('')}</div>
      <div class="comm-axis"><span>1</span><span>10</span></div>`);
    const counts = [['pulls', 'pull'], ['collected', 'have'], ['read', 'read'], ['wanted', 'want']]
      .filter(([f]) => locg[f] != null).map(([f, l]) => `<span><b>${locg[f].toLocaleString()}</b> ${l}</span>`);
    if (counts.length) more.push(`<div class="comm-counts">${counts.join('')}</div>`);
  }
  if (cbr) more.push(`<div>ComicBookRoundup · ${cbr.critic_reviews || 0} critic reviews · ${cbr.user_reviews || 0} reader reviews</div>`);
  if (hc && hc.rating != null) more.push(`<div>Hardcover ${hc.rating}/5 · ${(hc.count || 0).toLocaleString()} ratings</div>`);
  if (ol && ol.rating != null) more.push(`<div>Open Library ${ol.rating}/5 · ${(ol.count || 0).toLocaleString()} ratings</div>`);
  return `<div class="issue-modal-credit-row comm-row" onclick="const m=this.nextElementSibling; _animateModalHeight(() => { m.hidden = !m.hidden; })">
      <div class="issue-modal-credit-role u-label">Community</div>
      <div class="comm-scores">${line}</div>
    </div>
    <div class="comm-more" hidden>${more.join('')}</div>`;
}

// Long credit lists (_renderIssueDetails): over this many lines, writers and artists first.
const CREDITS_LONG = 15, CREDITS_ROW_NAMES = 6;
const _TEAM_ROLES = new Set(['writer', 'story', 'script', 'plot', 'artist', 'penciller', 'pencils', 'pencil', 'art',
  'layouts', 'breakdowns', 'illustrator', 'illustration', 'illustrations']);   // related.ROLE_GROUPS' writer + artist
function _isTeamRole(role) {
  return (role || '').toLowerCase().split(/\s*[,/&]\s*|\s+and\s+/).some(p => _TEAM_ROLES.has(p.trim()));
}

// '+N' opens the rest of its row; '+ N more credits' opens everything. The count on the
// last one follows what's still hidden.
function _creditsOpen(btn, all) {
  const box = btn.closest('.issue-modal-credits');
  _animateModalHeight(() => {
    if (all) {
      box.querySelectorAll('[data-rest]').forEach(e => { e.hidden = false; });
      box.querySelectorAll('.credit-more').forEach(b => b.remove());
      box.querySelector('.credit-more-row')?.remove();
      return;
    }
    btn.previousElementSibling.hidden = false;
    btn.remove();
    const more = box.querySelector('.credit-more-row .credit-more');
    if (!more) return;
    const left = [...box.querySelectorAll('[data-rest][hidden]')].reduce((n, e) => n + +e.dataset.rest, 0);
    if (left) more.textContent = `+ ${left} more credits`; else more.closest('.credit-more-row').remove();
  });
}

// The card's names were just sent to Metron for their ids: look again a few times, and
// redraw once a name has become a link — not only the next time the card opens.
function _creditIdsLater(seriesId, number, had, tries = 3) {
  const linked = cs => cs.filter(c => c.metron_creator_id).length;
  setTimeout(async () => {
    if (_issueVariantSeriesId !== seriesId || _issueVariantNumber !== number
        || document.getElementById('modal')?.classList.contains('hidden')) return;
    let d;
    try { d = await api.get(`/api/series/${seriesId}/issues/${number}/locg-details`); } catch { return; }
    if (linked(d.credits || []) > linked(had)) _renderIssueDetails(d.desc, d.credits || [], d.community);
    else if (tries > 1) _creditIdsLater(seriesId, number, had, tries - 1);
  }, 4000);
}

function _renderIssueDetails(desc, credits, community) {
  const el = document.getElementById('issue-modal-details');
  if (!el) return;
  let html = _communityHtml(community);
  if (desc) html += `<div class="issue-modal-desc">${esc(desc)}</div>`;
  if (credits && credits.length) {
    const grouped = {};
    for (const c of credits) {
      if (c.name) (grouped[c.role || 'Other'] = grouped[c.role || 'Other'] || []).push(c);
    }
    // a name with a Metron id is a door: tap it for everything by that person
    const person = c => c.metron_creator_id
      ? `<button class="credit-link" onclick="showCreatorModal(${c.metron_creator_id}, ${jsArg(c.name)})">${esc(c.name)}</button>`
      : esc(c.name);
    // A long list — an anthology or a collected edition: Judge Dredd: The Complete Case Files
    // #48 has 52 lines from GCD — shows its writers and artists, six names a row, and the rest
    // behind '+ N more credits'. Fifteen lines or fewer: all of it. Same on the apps (2026-10-17).
    const entries = Object.entries(grouped);
    const lines = entries.reduce((n, [, p]) => n + p.length, 0);
    const long = lines > CREDITS_LONG;
    const team = entries.filter(([role]) => _isTeamRole(role));
    const shown = !long ? entries : team.length ? team : entries.slice(0, 2);
    const row = ([role, people], hidden) => {
      const cap = long && !hidden ? CREDITS_ROW_NAMES : Infinity, rest = people.slice(cap);
      return `<div class="issue-modal-credit-row"${hidden ? ` hidden data-rest="${people.length}"` : ''}>
          <div class="issue-modal-credit-role u-label">${esc(role)}</div>
          <div class="issue-modal-credit-name">${people.slice(0, cap).map(person).join(', ')}${rest.length
            ? `<span hidden data-rest="${rest.length}">, ${rest.map(person).join(', ')}</span> <button class="credit-more" onclick="_creditsOpen(this, false)">+${rest.length}</button>`
            : ''}</div>
        </div>`;
    };
    const left = lines - shown.reduce((n, [, p]) => n + Math.min(p.length, CREDITS_ROW_NAMES), 0);
    html += '<div class="issue-modal-credits">' + shown.map(e => row(e, false)).join('')
      + (long ? entries.filter(e => !shown.includes(e)).map(e => row(e, true)).join('')
          + `<div class="issue-modal-credit-row credit-more-row"><button class="credit-more" onclick="_creditsOpen(this, true)">+ ${left} more credits</button></div>` : '')
      + '</div>';
  }
  _animateModalHeight(() => {
    el.innerHTML = html || '<div class="state-msg" style="font-size:11px;padding:8px 0;color:var(--tq)">No details available.</div>';
    el.style.animation = 'none'; void el.offsetWidth;   // restart the fade
    el.style.animation = 'fade-in var(--t-slow) var(--ease-out)';
  });
}

// '1994-08' → 'Cover date August 1994'; '1994' → '1994'
function _fmtCoverDate(c) {
  const m = /^(\d{4})-(\d{2})/.exec(c || '');
  if (!m) return (c || '').slice(0, 4);
  const d = new Date(+m[1], +m[2] - 1, 1);
  return `Cover date ${d.toLocaleDateString('en', { month: 'long', year: 'numeric' })}`;
}

async function showIssueModal(seriesId, number, opts = {}) {
  clearTimeout(_issueModalPollTimer);
  // Fast path: already viewing this series, its issues are cached on _detailSeries.
  // Called from elsewhere (an arc's cross-title reading order) — _detailSeries is
  // the ARC's payload, not this issue's own series — fetch it fresh instead of
  // silently no-op'ing, so the SAME real modal works from any context.
  // The cached series payload is only trusted WHILE on its page: left behind,
  // it goes stale (a download lands, the issue flips to owned) and On Deck's ⋯
  // would show 'Missing · Download' for a book you're holding.
  let homeSeries = currentView === 'series-detail' && _detailSeries?.id === seriesId ? _detailSeries : null;
  let issue = homeSeries?.issues?.find(i => i.number === number);
  if (!issue) {
    try {
      homeSeries = await api.get(`/api/series/${seriesId}`);
    } catch (e) {
      showToast('Couldn’t load issue — ' + (e?.message || e), 'error');
      return;
    }
    issue = homeSeries?.issues?.find(i => i.number === number);
    if (!issue) {
      // the run doesn't list it: a shelf book gets the book's card, a gap its run
      if (opts.book) return showBookModal(opts.book, opts);
      if (opts.orSeries) navigate('series-detail', { id: seriesId });
      return;
    }
  }

  // A card that hands us a book is on the shelf whatever the issue row says
  const st = opts.book ? 'owned' : issueStatus(issue);

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
  // carry no pref (it's baked into the CBZ already): the file's own cover, via the route.
  const imgSrc = issue.variant_cover
    ? issue.variant_cover
    : st === 'owned'
      ? esc(bookThumb(issue))
      : (_metronArt(issue)
          // same server-side fallback chain the grid tiles use — never show the
          // no-cover void when LOCG has variant art for an artless upcoming issue
          || `/api/series/${seriesId}/issues/${issue.number}/thumbnail?c=3`);

  const chipMap = {
    owned:   `<span class="chip chip-complete">Owned</span>`,
    missing: `<span class="chip chip-missing">Missing</span>`,
    upcoming:`<span class="chip chip-upcoming">Upcoming</span>`,
    today:   `<span class="chip chip-today">Today</span>`,
    unknown: `<span class="chip" style="color:var(--tq);border-color:var(--tq)">Unknown</span>`,
    ignored: `<span class="chip chip-ignored">Ignored</span>`,
  };

  let dateHtml = '';
  if (issue.store_date >= '9000') {
    dateHtml = `<div class="issue-modal-release-label">Release date TBD</div>`;   // the catalogue's 'unscheduled' placeholder
  } else if (issue.store_date) {
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
  } else if (issue.cover_date) {
    // no on-sale date anywhere: the month on the cover ('1994-08'), or just the year —
    // never passed off as a release date (kometa/gapfill.py)
    dateHtml = `<div class="issue-modal-release-label">${esc(_fmtCoverDate(issue.cover_date))}</div>`;
  }

  // The card's actions, the same wherever it opens (2026-10-17): Want to read and Go to
  // series always (Go to series not on that series' own page), then what the issue's
  // state allows — Mark as read and Read for one you have, Ignore and Download for one
  // that's out, Stop ignoring. No Close: outside click and Esc do it.
  const onItsPage = currentView === 'series-detail' && _detailSeries?.id === seriesId;
  const goSeries = onItsPage ? '' : `<button class="btn btn-ghost" onclick="navigate('series-detail', {id: ${seriesId}})">Go to series</button>`;
  let footerAction = '';
  if (st === 'owned') {
    // Mark as read waits for the book (its read state) unless the card handed it over.
    // Opened in the reader: its own Mark as read (the end screen), and no Read — you're in it
    const b = opts.book;
    footerAction = `
        <button class="btn btn-ghost" id="issue-read-state"${b ? '' : ' hidden'}
          onclick="closeModal(); ${_readStateCall(b && b.book_id, b && b.completed, opts.inReader)}">${b && b.completed ? 'Mark as unread' : 'Mark as read'}</button>
        ${opts.inReader ? '' : `<button class="btn btn-primary" onclick="${b ? `navigate('read', {book: ${b.book_id}})` : `openIssueReader(${seriesId}, ${number})`}">Read</button>`}`;
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
        <div style="margin:8px 0">${chipMap[st] || ''}${s && s.run_status
          ? ` <span class="chip chip-upcoming" title="${esc(s.run_status.why)}">${esc(s.run_status.label)}</span>` : ''}</div>
        ${dateHtml}
        <div id="issue-modal-own"></div>
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
            <div class="variant-hint" id="variant-hint">Click a cover to view it large — include or ${ico('star')} it from there.</div>
            <button class="btn btn-primary btn-sm" id="variant-apply-btn" disabled
              onclick="_imApplyVariants(${seriesId}, ${number}, ${st === 'owned' ? 'true' : 'false'})">Apply</button>
          </div>
        </div>` : ''}
      </div>
    </div>
    <div class="modal-footer" id="issue-modal-footer">
      ${_wantBtnHtml({ series_id: seriesId, number })}
      ${goSeries}
      ${footerAction}
    </div>
  `);

  // Owned: the 1–5 rating and the heart, the pages and where you're up to, and
  // Mark as read — all off the book behind the issue.
  if (st === 'owned') {
    const bookP = opts.book ? api.get(`/api/books/${opts.book.book_id}`) : api.get(`/api/series/${seriesId}/issues/${number}/book`);
    bookP.then(bk => {
      const host = document.getElementById('issue-modal-own');
      if (!host || !bk) return;
      const done = !!bk.progress?.completed;
      const where = done ? ' · read' : bk.progress ? ` · on page ${bk.progress.page}` : '';
      _animateModalHeight(() => {
        host.innerHTML = _markRowHtml(bk)
          + (bk.page_count ? `<div class="issue-modal-meta">${bk.page_count} pages${where}</div>` : '');
        const rs = document.getElementById('issue-read-state');
        if (rs) {
          rs.textContent = done ? 'Mark as unread' : 'Mark as read';
          rs.setAttribute('onclick', `closeModal(); ${_readStateCall(bk.id, done, opts.inReader)}`);
          rs.hidden = false;
        }
      });
    }).catch(() => {});
  }


  // Fetch issue details async from LOCG (keyless). Credits arrive as flat
  // [{role, name}] ready for _renderIssueDetails.
  if (hasLocgId) {
    try {
      const d = await api.get(`/api/series/${seriesId}/issues/${number}/locg-details`);
      _renderIssueDetails(d.desc, d.credits || [], d.community);
      if (d.ids_pending) _creditIdsLater(seriesId, number, d.credits || []);
    } catch { _renderIssueDetails('', []); }
  } else if (st === 'owned') {
    // no catalogue id: the file's own tags may still know (read local first; a 404 when it's off)
    try {
      const d = await api.get(`/api/series/${seriesId}/issues/${number}/locg-details`);
      if (d.desc || (d.credits || []).length || d.community) _renderIssueDetails(d.desc, d.credits || [], d.community);
    } catch {}
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
  if (n === 0) { hint.innerHTML = `Click a cover to view it large — include or ${ico('star')} it from there.`; return; }
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
  img.src = c.big || c.large || c.thumb;   // a stored bigger copy of the same image first
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
  inc.innerHTML = on ? ico('check') + ' Included' : 'Include';
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
    btn.disabled = true; btn.innerHTML = 'Done ' + ico('check');
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

// --- live pages -----------------------------------------------------------------
// The shelf's pages repaint themselves when something behind them moves — a search
// queued, a download landing, a book finished on another device. Buffy's page sat still
// after its pull list went on until a reload (the owner, 2026-10-18; On Deck, Library and
// the pull list the same). A cheap stamp every LIVE_MS; a repaint only when it moves,
// quietly (the reader's back-to-the-page path: same scroll, no 'Loading'), never under a
// window or while you're typing.
const LIVE_MS = 10000;
const _LIVE = {
  'series-detail': () => renderSeriesDetail(currentParams.id),
  ondeck: () => renderOnDeck(),
  library: () => _loadBrowsePage(),
  'pull-list': () => _renderPullListContent(),
};
let _live = { key: null, stamp: null };
async function _liveTick() {
  const key = _LIVE[currentView] ? `${currentView}:${currentParams?.id ?? ''}` : null;
  if (!key || document.hidden) { _live = { key: null, stamp: null }; return; }
  if (!document.getElementById('modal')?.classList.contains('hidden')) return;
  if (document.activeElement?.matches('input, textarea, select')) return;
  let stamp;
  try { stamp = (await api.get('/api/changes')).stamp; } catch { return; }
  const moved = _live.key === key && _live.stamp !== null && stamp !== _live.stamp;
  _live = { key, stamp };
  if (!moved) return;
  const gen = _vt.gen;
  _vt.quiet = { gen, y: _appScrollTop() };
  try { await _LIVE[currentView](); } catch {} finally { if (_vt.quiet?.gen === gen) _vt.quiet = null; }
}
setInterval(_liveTick, LIVE_MS);

// --- pull-to-refresh (touch only) ---
// Re-fetches the current view's DATA without resetting filters/search state.
const _PTR_VIEWS = new Set(['library', 'stack', 'ondeck', 'needs-match', 'series-detail', 'pull-list', 'activity']);
let _ptrStartY = 0, _ptrPulling = false;

function _ptrRefresh() {
  // The gesture means "make it FRESH", not just "repaint the cache" — where
  // the data is born (library, series detail), a pull also kicks a real sync
  // (LOCG/Metron/folder rescan). Repaint is instant; the sync lands behind it.
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
  // Always land on the library — never a blocking welcome gate. Every
  // integration is optional and configured in Settings: search and track via
  // Metron/LOCG, own via folders.
  const cfg = await api.get('/api/config');
  _appConfig = cfg;
  const { view, params } = _parseHash();
  navigate(view, params);
  _startBadgePolling();
  // the Needs matching item's count: the same fetch the Library makes, shared — not a second one
  _libFetch().catch(() => {});
}

boot();


// --- Favourites + ratings (kometa/marks.py) --------------------------------------
// A star on anything, 1–5 on an issue. Reading state: keyed by row id, not path.
function _favBtn(s) {
  const r = s.rating || s.rating_derived;
  return `<button class="btn btn-sm btn-ghost fav-btn${s.favourite ? ' on' : ''}" id="fav-btn-${s.id}" title="${s.favourite ? 'Favourite — tap to remove' : 'Favourite'}"
    onclick="_toggleSeriesFav(${s.id}, this)">${ico('heart', !s.favourite)}${r ? ` <span class="fav-rating">${ico('star')} ${r}</span>` : ''}</button>`;
}

// --- Want to read (kometa/wantread.py) -------------------------------------------
// One toggle for an issue, a book or a whole series; lit when it's on the list.
function _wantBtnHtml(target, cls = '') {
  return `<button class="btn btn-ghost want-btn ${cls}" data-want="${esc(JSON.stringify(target))}" onclick="_wantToggle(this)" disabled>Want to read</button>`;
}

function _wantPaint(btn, id) {
  btn.dataset.id = id || '';
  btn.classList.toggle('on', !!id);
  btn.innerHTML = id ? ico('check') + ' Want to read' : 'Want to read';
  btn.title = id ? 'On your Want to read — tap to take it off' : 'Add to Want to read on On Deck';
}

async function _wantSync(root = document) {
  for (const btn of root.querySelectorAll('.want-btn')) {
    const q = new URLSearchParams(Object.entries(JSON.parse(btn.dataset.want)).filter(([, v]) => v != null));
    try { const r = await api.get(`/api/wantread/check?${q}`); _wantPaint(btn, r.want_id); btn.disabled = false; } catch {}
  }
}

async function _wantToggle(btn) {
  const id = btn.dataset.id;
  btn.disabled = true;
  try {
    if (id) { await api.del(`/api/wantread/${id}`); _wantPaint(btn, null); showToast('Off Want to read'); }
    else { const r = await api.post('/api/wantread', JSON.parse(btn.dataset.want)); _wantPaint(btn, r.want_id); showToast('On Want to read'); }
  } catch { showToast('Couldn’t save that', 'error'); }
  btn.disabled = false;
}

async function _toggleSeriesFav(id, btn) {
  const on = !btn.classList.contains('on');
  btn.classList.toggle('on', on); btn.querySelector('.ico-heart').outerHTML = ico('heart', !on);
  try { await api.put(`/api/series/${id}/mark`, { favourite: on }); if (_detailSeries?.id === id) _detailSeries.favourite = on; }
  catch { btn.classList.toggle('on', !on); btn.querySelector('.ico-heart').outerHTML = ico('heart', on); showToast('Couldn’t save that', 'error'); }
}

function _markRowHtml(bk) {
  // Rating on the left, favourite (a heart) on the right, nothing in between —
  // six stars in a row read as one control. Tapping the lit star again clears.
  const dots = [1, 2, 3, 4, 5].map(n => `<button class="rating-dot${bk.rating >= n ? ' on' : ''}" data-n="${n}" title="${n} of 5" aria-label="Rate ${n}"
      onclick="_setBookMark(${bk.id}, ${bk.rating === n ? '{clear_rating: true}' : `{rating: ${n}}`}, this.closest('.mark-row'))">${ico('star')}</button>`).join('');
  return `<div class="mark-row" id="mark-row-${bk.id}">
    <span class="rating-dots">${dots}</span>
    <button class="mark-heart${bk.favourite ? ' on' : ''}" title="${bk.favourite ? 'Favourited — tap to remove' : 'Favourite'}" aria-label="Favourite" aria-pressed="${!!bk.favourite}"
      onclick="_setBookMark(${bk.id}, {favourite: !this.classList.contains('on')}, this.closest('.mark-row'))">${ico('heart', !bk.favourite)}</button>
  </div>`;
}

async function _setBookMark(bookId, patch, row) {
  try {
    const r = await api.put(`/api/books/${bookId}/mark`, patch);
    if (row) row.outerHTML = _markRowHtml({ id: bookId, ...r });
  } catch { showToast('Couldn’t save that', 'error'); }
}


// Sign out: this browser's session only (POST /api/auth/logout revokes its token and
// drops the cookie), then the sign-in page. Shown only when login is on — with it
// off, everyone is the admin and there's nothing to sign out of.
async function signOut() {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch {}
  location.replace('/');
}
(async () => {
  try {
    const me = await (await fetch('/api/auth/me')).json();
    if (me.auth_enabled) document.getElementById('nav-signout')?.removeAttribute('hidden');
  } catch {}
})();
