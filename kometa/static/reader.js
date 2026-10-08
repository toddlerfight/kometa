// --- Reader ------------------------------------------------------------------
// The web half of docs/reader-spec.md. A route of its own (#read?book=42), so
// Back, reload and a bookmarked URL all land you on the page you were on.
//
// Layout: portrait = one page. Landscape (wide enough) = two-up, with the cover
// and wide spreads always alone. "Shift pairing" fixes files whose ads/missing
// pages knock every spread off by one. Controls hide while reading; a tap in the
// middle brings them back. Edges (or a swipe, or the arrow keys) turn pages.
//
// Depends on app.js globals: api, esc, showToast, navigate, currentParams.

const _rd = {
  book: null,        // /api/books/{id} payload
  spreads: [],       // [[pageNo], [pageNo, pageNo], ...] — 1-based page numbers
  at: 0,             // index into spreads
  mode: 'auto',      // 'auto' | 'single' | 'double'
  shift: false,      // shift pairing by one after the cover
  chrome: false,
  ended: false,
  saveTimer: null,
  lastSaved: null,
  touch: null,
};

const _RD_MODE_KEY = 'kometa.reader.mode';
try { _rd.mode = localStorage.getItem(_RD_MODE_KEY) || 'auto'; } catch {}

function _rdEl() {
  let el = document.getElementById('reader');
  if (!el) {
    el = document.createElement('div');
    el.id = 'reader';
    el.className = 'hidden';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Comic reader');
    document.body.appendChild(el);
  }
  return el;
}

function _rdTwoUp() {
  if (_rd.mode === 'single') return false;
  if (_rd.mode === 'double') return true;
  return window.innerWidth > window.innerHeight && window.innerWidth >= 900;
}

// Pages that never pair: the cover, and anything drawn as a single wide image.
function _rdBuildSpreads() {
  const pages = _rd.book.pages;
  const n = pages.length;
  const out = [];
  if (!_rdTwoUp()) {
    for (let p = 1; p <= n; p++) out.push([p]);
    return out;
  }
  let p = 1;
  out.push([p++]);                               // cover alone
  if (_rd.shift && p <= n) out.push([p++]);      // knock pairing over by one
  while (p <= n) {
    if (pages[p - 1].wide) { out.push([p++]); continue; }
    if (p + 1 <= n && !pages[p].wide) { out.push([p, p + 1]); p += 2; }
    else out.push([p++]);
  }
  return out;
}

function _rdSpreadOf(page) {
  const i = _rd.spreads.findIndex(s => s.includes(page));
  return i < 0 ? 0 : i;
}

function _rdPageUrl(page, cssWidth) {
  const w = Math.round(cssWidth * Math.min(window.devicePixelRatio || 1, 2));
  return `/api/books/${_rd.book.id}/pages/${page}?w=${w}&v=${encodeURIComponent(_rd.book.version)}`;
}

function _rdSlotWidth(count) {
  return count === 2 ? window.innerWidth / 2 : window.innerWidth;
}

function _rdPreload() {
  for (const d of [1, 2, -1]) {
    const s = _rd.spreads[_rd.at + d];
    if (!s) continue;
    for (const p of s) { const im = new Image(); im.src = _rdPageUrl(p, _rdSlotWidth(s.length)); }
  }
}

function _rdTitle() {
  const b = _rd.book;
  return b.number != null ? `${b.title} #${fmtNum(b.number)}` : b.title;
}

function _rdRender() {
  const el = _rdEl();
  const b = _rd.book;
  if (_rd.ended) return _rdRenderEnd();
  const spread = _rd.spreads[_rd.at] || [1];
  const last = spread[spread.length - 1];
  const imgs = spread.map(p => `
    <img class="rd-page${spread.length === 2 ? (p === spread[0] ? ' rd-left' : ' rd-right') : ''}"
      src="${_rdPageUrl(p, _rdSlotWidth(spread.length))}" alt="Page ${p}" draggable="false">`).join('');
  const label = spread.length === 2 ? `${spread[0]}–${spread[1]}` : `${spread[0]}`;
  const thumbs = b.pages.map((_, i) => {
    const p = i + 1, on = spread.includes(p);
    return `<button class="rd-thumb${on ? ' on' : ''}" data-page="${p}" aria-label="Page ${p}" onclick="_rdGo(${p})">
      <img src="${_rdPageUrl(p, 120)}" alt="" loading="lazy" draggable="false"><span>${p}</span></button>`;
  }).join('');
  el.innerHTML = `
    <div class="rd-stage${spread.length === 2 ? ' rd-two' : ''}" id="rd-stage">${imgs}</div>
    <div class="rd-progress" style="width:${(last / b.page_count) * 100}%"></div>
    <div class="rd-count" aria-hidden="true">${label} / ${b.page_count}</div>
    <div class="rd-chrome${_rd.chrome ? ' on' : ''}" id="rd-chrome">
      <div class="rd-top">
        <div class="rd-top-l">
          <button class="rd-back" onclick="_rdExit()">‹ Back</button>
          <div class="rd-titles">
            <div class="rd-title">${esc(_rdTitle())}</div>
            <div class="rd-sub">${esc((b.publisher || '').toUpperCase())}${b.publisher ? ' · ' : ''}${b.page_count} PAGES</div>
          </div>
        </div>
        <div class="rd-top-r">
          <button class="rd-btn" onclick="_rdCycleMode()" title="Page layout" aria-label="Page layout">${{ auto: 'AUTO', single: '1-UP', double: '2-UP' }[_rd.mode]}</button>
          <button class="rd-btn" onclick="_rdToggleMenu(event)" aria-label="More">⋯</button>
          <div class="rd-menu hidden" id="rd-menu">
            <button onclick="_rdToggleShift()">${_rd.shift ? '✓ ' : ''}Shift pairing by one</button>
            <button onclick="_rdMarkRead()">Mark as read</button>
          </div>
        </div>
      </div>
      <div class="rd-bottom">
        <div class="rd-strip" id="rd-strip">${thumbs}</div>
        <div class="rd-scrub">
          <span>${spread[0]}</span>
          <input type="range" min="1" max="${b.page_count}" value="${spread[0]}" aria-label="Page"
            oninput="this.previousElementSibling.textContent=this.value" onchange="_rdGo(+this.value)">
          <span>${b.page_count}</span>
        </div>
      </div>
    </div>`;
  if (_rd.chrome) _rdCenterStrip();
  _rdPreload();
  _rdQueueSave(last);
}

function _rdRenderEnd() {
  const el = _rdEl();
  const b = _rd.book;
  el.innerHTML = `
    <div class="rd-end">
      <div class="rd-end-done">✓ FINISHED</div>
      <div class="rd-end-title">${esc(_rdTitle())}</div>
      <div class="rd-end-actions">
        <button class="btn btn-ghost" onclick="_rdEnded(false)">‹ Last page</button>
        <button class="btn btn-primary" onclick="_rdExit()">Done</button>
      </div>
      <div class="rd-end-note">Up next arrives with On Deck.</div>
    </div>`;
  _rdQueueSave(b.page_count, true);
}

function _rdCenterStrip() {
  const on = document.querySelector('#rd-strip .rd-thumb.on');
  if (on) on.scrollIntoView({ block: 'nearest', inline: 'center' });
}

// --- navigation ----------------------------------------------------------------

function _rdGo(page) {
  _rd.ended = false;
  _rd.at = _rdSpreadOf(Math.max(1, Math.min(page, _rd.book.page_count)));
  _rdRender();
}

function _rdStep(dir) {
  if (_rd.ended) { if (dir < 0) _rdEnded(false); return; }
  const next = _rd.at + dir;
  if (next >= _rd.spreads.length) return _rdEnded(true);
  if (next < 0) return;
  _rd.at = next;
  _rdRender();
}

function _rdEnded(on) {
  _rd.ended = on;
  if (!on) _rd.at = _rd.spreads.length - 1;
  _rdRender();
}

function _rdToggleChrome(force) {
  _rd.chrome = force ?? !_rd.chrome;
  document.getElementById('rd-chrome')?.classList.toggle('on', _rd.chrome);
  document.getElementById('rd-menu')?.classList.add('hidden');
  if (_rd.chrome) _rdCenterStrip();
}

function _rdCycleMode() {
  const cur = _rd.spreads[_rd.at]?.[0] || 1;
  _rd.mode = { auto: 'single', single: 'double', double: 'auto' }[_rd.mode];
  try { localStorage.setItem(_RD_MODE_KEY, _rd.mode); } catch {}
  _rd.spreads = _rdBuildSpreads();
  _rd.at = _rdSpreadOf(cur);
  _rdRender();
}

function _rdToggleShift() {
  const cur = _rd.spreads[_rd.at]?.[0] || 1;
  _rd.shift = !_rd.shift;
  _rd.spreads = _rdBuildSpreads();
  _rd.at = _rdSpreadOf(cur);
  _rdRender();
}

function _rdToggleMenu(e) {
  e.stopPropagation();
  document.getElementById('rd-menu')?.classList.toggle('hidden');
}

// --- progress --------------------------------------------------------------------
// Explicit writes, debounced: flipping through ten pages is one save, not ten.
// The server refuses anything older than what it holds (409) — that's another
// device being further along, and it wins.

function _rdQueueSave(page, completed) {
  clearTimeout(_rd.saveTimer);
  _rd.saveTimer = setTimeout(() => _rdSave(page, completed), 1200);
}

async function _rdSave(page, completed) {
  if (!_rd.book) return;
  const key = `${page}:${!!completed}`;
  if (_rd.lastSaved === key) return;
  const body = { page, updated_at: new Date().toISOString() };
  if (completed) body.completed = true;
  try {
    await api.put(`/api/books/${_rd.book.id}/progress`, body);
    _rd.lastSaved = key;
  } catch (e) {
    // stale (another device is ahead) or offline — progress is best-effort here
  }
}

async function _rdMarkRead() {
  clearTimeout(_rd.saveTimer);
  await _rdSave(_rd.book.page_count, true);
  showToast('Marked as read');
  _rdToggleChrome(false);
}

// --- input -------------------------------------------------------------------------

function _rdZoomed() {
  return window.visualViewport && window.visualViewport.scale > 1.05;
}

function _rdOnTap(x) {
  const w = window.innerWidth;
  if (_rd.chrome) return _rdToggleChrome(false);
  if (x < w * 0.3) _rdStep(-1);
  else if (x > w * 0.7) _rdStep(1);
  else _rdToggleChrome(true);
}

function _rdBindInput() {
  const el = _rdEl();
  if (el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('click', e => {
    if (_rd.ended || e.target.closest('.rd-chrome .rd-top, .rd-chrome .rd-bottom, .rd-end')) return;
    if (_rdZoomed()) return;
    _rdOnTap(e.clientX);
  });
  el.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) { _rd.touch = null; return; }
    _rd.touch = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() };
  }, { passive: true });
  el.addEventListener('touchend', e => {
    const t = _rd.touch; _rd.touch = null;
    if (!t || _rdZoomed() || _rd.chrome) return;
    const dx = e.changedTouches[0].clientX - t.x, dy = e.changedTouches[0].clientY - t.y;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5 && Date.now() - t.t < 600) {
      e.preventDefault();               // a swipe, not a tap — don't let click fire
      _rdStep(dx < 0 ? 1 : -1);
    }
  });
  document.addEventListener('keydown', e => {
    if (currentView !== 'read' || !_rd.book) return;
    if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') { e.preventDefault(); _rdStep(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); _rdStep(-1); }
    else if (e.key === 'Escape') { e.preventDefault(); _rd.chrome ? _rdToggleChrome(false) : _rdExit(); }
  });
  let rt;
  window.addEventListener('resize', () => {
    if (currentView !== 'read' || !_rd.book) return;
    clearTimeout(rt);
    rt = setTimeout(() => {
      const cur = _rd.spreads[_rd.at]?.[0] || 1;
      _rd.spreads = _rdBuildSpreads();
      _rd.at = _rdSpreadOf(cur);
      _rdRender();
    }, 150);
  });
}

// --- open / close --------------------------------------------------------------------

async function renderReader(params) {
  const el = _rdEl();
  el.classList.remove('hidden');
  document.body.classList.add('reading');
  el.innerHTML = '<div class="rd-loading">Opening…</div>';
  _rdBindInput();
  try {
    const book = await api.get(`/api/books/${params.book}`);
    Object.assign(_rd, { book, shift: false, chrome: false, ended: false, lastSaved: null });
    _rd.spreads = _rdBuildSpreads();
    const p = book.progress;
    const start = params.page ? +params.page : (p && !p.completed ? p.page : 1);
    _rd.at = _rdSpreadOf(start);
    _rdRender();
  } catch (e) {
    el.innerHTML = `<div class="rd-end"><div class="rd-end-title">Couldn’t open this book</div>
      <div class="rd-end-actions"><button class="btn btn-primary" onclick="_rdExit()">‹ Back</button></div></div>`;
  }
}

function closeReader() {
  const el = document.getElementById('reader');
  if (!el || el.classList.contains('hidden')) return;
  if (_rd.book && _rd.saveTimer) {
    clearTimeout(_rd.saveTimer);
    const s = _rd.spreads[_rd.at];
    if (s && !_rd.ended) _rdSave(s[s.length - 1]);
  }
  el.classList.add('hidden');
  el.innerHTML = '';
  document.body.classList.remove('reading');
  _rd.book = null;
}

function _rdExit() {
  const from = _rd.book?.series_id;
  if (history.length > 1 && history.state && history.state.view === 'read') history.back();
  else if (from) navigate('series-detail', { id: from });
  else navigate('library');
}

// The Read button: issue → book id → the reader.
async function openIssueReader(seriesId, number) {
  try {
    const book = await api.get(`/api/series/${seriesId}/issues/${number}/book`);
    if (typeof closeModal === 'function') closeModal();
    navigate('read', { book: book.id });
  } catch (e) {
    showToast('Couldn’t open this issue — ' + (e?.message || e), 'error');
  }
}

// --- Shelf series (untracked) --------------------------------------------------
// A series Kometa doesn't track: on the shelf, readable, nothing to fetch. Books
// in issue order, Kometa's own covers, your progress on each.

function _shelfNextBook(books) {
  const going = books.filter(b => b.progress && !b.progress.completed)
    .sort((a, b) => (b.progress.updated_at || '').localeCompare(a.progress.updated_at || ''))[0];
  if (going) return { book: going, label: `Continue ${going.label}` };
  const lastRead = books.map((b, i) => (b.progress?.completed ? i : -1)).reduce((a, b) => Math.max(a, b), -1);
  const next = books[lastRead + 1] || books[0];
  return next ? { book: next, label: lastRead >= 0 ? `Read ${next.label}` : `Start ${next.label}` } : null;
}

async function renderShelfSeries(id) {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');
  const s = await api.get(`/api/shelf/${id}`);
  if (currentView !== 'shelf' || currentParams.id !== id) return;
  document.getElementById('topbar-title').textContent = s.title;
  const read = s.books.filter(b => b.progress?.completed).length;
  document.getElementById('topbar-sub').innerHTML = `<span class="u-label" style="color:var(--tq)">
    ${esc((s.publisher || '').toUpperCase())} · ${s.books.length} BOOK${s.books.length === 1 ? '' : 'S'}${read ? ` · ${read} READ` : ''} · NOT TRACKED</span>`;
  const nx = _shelfNextBook(s.books);
  if (nx) document.getElementById('topbar-actions').innerHTML =
    `<button class="btn btn-primary btn-sm" onclick="navigate('read', {book: ${nx.book.id}})">${esc(nx.label)}</button>`;
  const tiles = s.books.map(b => {
    const p = b.progress;
    const mark = p?.completed ? '<div class="shelf-tile-done" aria-label="Read">✓</div>'
      : p ? `<div class="shelf-tile-bar"><div style="width:${b.page_count ? Math.round(p.page / b.page_count * 100) : 10}%"></div></div>` : '';
    const go = `navigate('read', {book: ${b.id}})`;
    return `<div class="issue-tile${p?.completed ? ' shelf-tile-read' : ''}" tabindex="0" role="button" title="${esc(s.title)} ${esc(b.label)}"
        onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
      <div class="issue-tile-img"><img src="/api/books/${b.id}/cover" alt="${esc(b.label)}" loading="lazy"
        onerror="this.parentElement.classList.add('unknown');this.remove()">${mark}</div>
      <div class="issue-tile-num">${esc(b.label)}</div>
    </div>`;
  }).join('');
  setApp(s.books.length
    ? `<div class="issue-grid">${tiles}</div>`
    : '<div class="state-msg">No readable books in this folder.</div>');
}

// --- Series page: matching + files on the shelf ---------------------------------

async function _loadMatchCandidates(seriesId, q) {
  const box = document.getElementById('match-results');
  if (!box) return;
  box.innerHTML = '<div class="state-msg" style="padding:10px 0;font-size:11px">Searching…</div>';
  // Metron first (the API built for this), LOCG alongside when it's open to us.
  // Either can be shut; whatever answers is what you pick from.
  const enc = encodeURIComponent(q);
  const [metron, locg] = await Promise.all([
    api.get(`/api/search/metron?q=${enc}`).catch(() => []),
    api.get(`/api/search/locg?q=${enc}`).then(r => r.filter(x => !x.needs_resolve)).catch(() => []),
  ]);
  if (currentView !== 'series-detail' || currentParams.id !== seriesId) return;
  const rows = metron.map(r => ({ ...r, source: 'metron' })).slice(0, 8)
    .concat(locg.map(r => ({ ...r, source: 'locg' })).slice(0, 8));
  box.innerHTML = rows.length ? rows.map(r => `
    <div class="match-row">
      ${r.cover ? `<img src="${esc(r.cover)}" alt="" loading="lazy">` : '<div class="match-nocover"></div>'}
      <div class="match-row-text"><div>${esc(r.series)}</div>
        <div class="u-label" style="color:var(--tq)">${esc([r.publisher?.name, r.year_began, r.issue_count ? `${r.issue_count} issues` : null].filter(Boolean).join(' · '))}
          <span class="match-source">${r.source === 'metron' ? 'Metron' : 'LOCG'}</span></div></div>
      <button class="btn btn-primary btn-sm" onclick="_pickMatch(${seriesId}, ${r.id}, this, '${r.source}')">This one</button>
    </div>`).join('')
    : '<div class="state-msg" style="padding:10px 0;font-size:11px">No series found — try a different search.</div>';
}

async function _pickMatch(seriesId, runId, btn, source = 'locg') {
  if (btn) { btn.disabled = true; btn.textContent = 'Linking…'; }
  try {
    await api.patch(`/api/series/${seriesId}/locg`, source === 'metron' ? { metron_id: runId } : { locg_id: runId });
    showToast('Linked — fetching its issues, trades and covers');
    renderSeriesDetail(seriesId);
  } catch (e) {
    showToast('Couldn’t link that run'); if (btn) { btn.disabled = false; btn.textContent = 'This one'; }
  }
}

// Files on the shelf that aren't numbered issues (trades, omnibuses, oddities) —
// or, for a series with no issue list at all, every file — so nothing on disk is
// unreadable just because it doesn't fit the issue grid.
async function _loadShelfFiles(s, all) {
  let shelf;
  try { shelf = await api.get(`/api/shelf/${s.shelf_id}`); } catch { return; }
  if (currentView !== 'series-detail' || currentParams.id !== s.id) return;
  const books = all ? shelf.books : shelf.books.filter(b => b.number == null);
  if (!books.length) return;
  const tiles = books.map(b => {
    const p = b.progress;
    const mark = p?.completed ? '<div class="shelf-tile-done" aria-label="Read">✓</div>'
      : p ? `<div class="shelf-tile-bar"><div style="width:${b.page_count ? Math.round(p.page / b.page_count * 100) : 10}%"></div></div>` : '';
    const go = `navigate('read', {book: ${b.id}})`;
    return `<div class="issue-tile${p?.completed ? ' shelf-tile-read' : ''}" tabindex="0" role="button" title="${esc(b.label)}"
        onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
      <div class="issue-tile-img"><img src="/api/books/${b.id}/cover" alt="${esc(b.label)}" loading="lazy"
        onerror="this.parentElement.classList.add('unknown');this.remove()">${mark}</div>
      <div class="issue-tile-num">${esc(b.label)}</div></div>`;
  }).join('');
  const app = document.getElementById('app');
  app.querySelector('.shelf-files')?.remove();
  if (all) app.querySelector('.issue-grid')?.remove();
  app.insertAdjacentHTML('beforeend', `<div class="shelf-files">
    <div class="u-label shelf-files-head">${all ? 'On the shelf' : 'Also on the shelf'}</div>
    <div class="issue-grid">${tiles}</div></div>`);
}

async function _matchNow(seriesId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Matching…'; }
  try {
    const r = await fetch(`/api/series/${seriesId}/match`, { method: 'POST' });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.status);
    showToast(body.match_status === 'auto' ? 'Matched — fetching issues, trades and covers'
      : 'More than one run could fit — pick it below');
    renderSeriesDetail(seriesId);
  } catch (e) {
    showToast(String(e.message || e), 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Match now'; }
  }
}

// LOCG has paused us (it refused requests): say so on the banner instead of
// offering buttons that can only fail.
async function _showLocgPause(seriesId) {
  let st;
  try { st = await api.get('/api/locg/status'); } catch { return; }
  if (!st.paused_until || currentView !== 'series-detail' || currentParams.id !== seriesId) return;
  const banner = document.getElementById('match-banner');
  if (!banner) return;
  banner.querySelectorAll('button').forEach(b => { b.disabled = true; });   // (the test button is added after)
  if (!banner.querySelector('.match-paused')) {
    banner.insertAdjacentHTML('beforeend', `<div class="match-paused u-label">LOCG is pausing us until
      ${esc(st.paused_until_label)} — it refused our requests. Matching resumes after that.
      <button class="btn btn-ghost btn-sm" style="margin-left:8px" onclick="_testLocg(this)">Test LOCG now</button></div>`);
  }
}

async function _testLocg(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
  try {
    const r = await api.post('/api/locg/test', {});
    showToast(r.detail, r.ok ? '' : 'error');
    if (r.ok && currentView === 'series-detail') renderSeriesDetail(currentParams.id);
  } catch (e) { showToast('Test failed — is the server up?', 'error'); }
  if (btn) { btn.disabled = false; btn.textContent = 'Test LOCG now'; }
}
