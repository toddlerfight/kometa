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
  const outgoing = document.getElementById('rd-stage');     // the spread you're leaving
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
          <button class="rd-btn" onclick="_rdFullscreen()" title="Full screen (F)" aria-label="Full screen">⛶</button>
          <button class="rd-btn" onclick="_rdCycleMode()" title="Page layout" aria-label="Page layout">${{ auto: 'AUTO', single: '1-UP', double: '2-UP' }[_rd.mode]}</button>
          <button class="rd-btn" onclick="_rdToggleMenu(event)" aria-label="More">⋯</button>
          <div class="rd-menu hidden" id="rd-menu">
            <button onclick="_rdToggleShift()">${_rd.shift ? '✓ ' : ''}Shift pairing by one</button>
            <button onclick="_rdMarkRead()">Mark as read</button>
            <button onclick="_rdToggleFav()">${_rd.book.favourite ? '♥ Favourited' : '♡ Favourite'}</button>
            ${(_rd.book.series_id || _rd.book.shelf_id) ? `<button onclick="_rdGoSeries()">Go to series</button>` : ''}
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
  _rdTurnMotion(outgoing);
  _rdPreload();
  _rdQueueSave(last);
}

// Same language as YondeB: a tap or key crossfades, a swipe pushes the new
// spread in from the side you swiped from while the old one slides out ahead
// of it. The outgoing stage is the SAME element the last render built — it is
// re-attached underneath so its pages are still decoded and nothing refetches.
// Reduced motion kills both in CSS; the classes are harmless then.
function _rdTurnMotion(outgoing) {
  const turn = _rd.turn; _rd.turn = null;
  const stage = document.getElementById('rd-stage');
  if (!turn || !stage || !outgoing || !outgoing.querySelector('img')) return;
  const swipe = turn.by === 'swipe';
  stage.classList.add(swipe ? (turn.dir > 0 ? 'rd-in-r' : 'rd-in-l') : 'rd-in-fade');
  outgoing.id = '';
  outgoing.className = 'rd-stage rd-out ' + (swipe ? (turn.dir > 0 ? 'rd-out-l' : 'rd-out-r') : 'rd-out-fade');
  stage.parentNode.insertBefore(outgoing, stage);
  const done = () => outgoing.remove();
  outgoing.addEventListener('animationend', done, { once: true });
  setTimeout(done, 400);                                 // reduced motion never fires animationend
}

function _rdRenderEnd() {
  const el = _rdEl();
  const b = _rd.book;
  el.innerHTML = `
    <div class="rd-end">
      <div class="rd-end-done card-cascade">✓ FINISHED</div>
      <div class="rd-end-title card-cascade" style="animation-delay:${STAGGER_MS}ms">${esc(_rdTitle())}</div>
      <div class="rd-end-marks card-cascade" style="animation-delay:${2 * STAGGER_MS}ms">${_markRowHtml(b)}</div>
      <div class="rd-end-next" id="rd-end-next"></div>
      <div class="rd-end-actions card-cascade" style="animation-delay:${3 * STAGGER_MS}ms">
        <button class="btn btn-ghost" onclick="_rdEnded(false)">‹ Last page</button>
        <button class="btn btn-primary" onclick="_rdExit()">Done</button>
      </div>
      <div class="rd-end-note" id="rd-end-note">${_rd.list ? 'Looking for the next on the list…' : 'Looking for the next issue…'}</div>
    </div>`;
  _rdQueueSave(b.page_count, true);
  // From a list the list's next is primary; otherwise the series' own next.
  if (_rd.list) _rdListNext(_rd.list, b.id); else _rdSeriesNext(b.id);
}

// Netflix-style: the next book's cover and a countdown that opens it. Only
// when the next book is on the shelf; any tap or key cancels. Never starts
// anywhere but this screen.
const RD_COUNTDOWN_S = 6;
async function _rdSeriesNext(bookId, secondary = false) {
  let n = null;
  try { n = await api.get(`/api/books/${bookId}/next`); } catch {}
  if (!_rd.book || _rd.book.id !== bookId || !_rd.ended) return;
  const note = document.getElementById('rd-end-note');
  const host = document.getElementById('rd-end-next');
  if (!n) { if (note && !secondary) note.textContent = ''; return; }
  if (n.status !== 'book') {
    if (note && !secondary) note.textContent = n.label;
    else if (note && secondary && !note.textContent) note.textContent = `In the series: ${n.label}`;
    return;
  }
  if (secondary) {
    document.querySelector('.rd-end-actions')?.insertAdjacentHTML('beforeend',
      `<button class="btn btn-ghost" onclick="navigate('read', {book: ${n.book_id}})">Next in series: ${esc(n.label)} ›</button>`);
    return;
  }
  if (note) note.textContent = '';
  host.innerHTML = `
    <div class="rd-next-card" role="button" tabindex="0" onclick="_rdOpenNext(${n.book_id})" onkeydown="if(event.key==='Enter')_rdOpenNext(${n.book_id})">
      <img class="rd-next-cover" src="/api/books/${n.book_id}/cover" alt="">
      <div class="rd-next-text"><div class="u-label" style="color:var(--tq)">Up next</div>
        <div class="rd-next-title">${esc(n.title)} ${esc(n.label)}</div>
        <div class="rd-next-count" id="rd-next-count">Starting in ${RD_COUNTDOWN_S}…</div></div>
    </div>`;
  _rdStartCountdown(n.book_id);
}

function _rdStartCountdown(nextId) {
  _rdCancelCountdown();
  let left = RD_COUNTDOWN_S;
  const tick = () => {
    left -= 1;
    const c = document.getElementById('rd-next-count');
    if (!_rd.ended || !c) return _rdCancelCountdown();
    if (left <= 0) { _rdCancelCountdown(); _rdOpenNext(nextId); return; }
    c.textContent = `Starting in ${left}…`;
  };
  _rd.countdown = setInterval(tick, 1000);
  // any touch, click or key anywhere cancels — capture phase, before handlers
  _rd.cancelCountdown = () => {
    _rdCancelCountdown();
    const c = document.getElementById('rd-next-count');
    if (c) c.textContent = 'Tap to read';
  };
  _rdEl().addEventListener('pointerdown', _rd.cancelCountdown, { capture: true, once: true });
  document.addEventListener('keydown', _rd.cancelCountdown, { capture: true, once: true });
}

function _rdCancelCountdown() {
  if (_rd.countdown) { clearInterval(_rd.countdown); _rd.countdown = null; }
  if (_rd.cancelCountdown) {
    _rdEl().removeEventListener('pointerdown', _rd.cancelCountdown, { capture: true });
    document.removeEventListener('keydown', _rd.cancelCountdown, { capture: true });
    _rd.cancelCountdown = null;
  }
}

function _rdOpenNext(id) {
  _rdCancelCountdown();
  navigate('read', _rd.list ? { book: id, list: _rd.list } : { book: id });
}

async function _rdToggleFav() {
  const b = _rd.book;
  try {
    const r = await api.put(`/api/books/${b.id}/mark`, { favourite: !b.favourite });
    b.favourite = r.favourite; b.rating = r.rating;
    showToast(r.favourite ? 'Favourited' : 'Removed from favourites');
    document.getElementById('rd-menu')?.classList.add('hidden');
  } catch { showToast('Couldn’t save that', 'error'); }
}

// From a reading list, 'next' is the list's next, not the series'.
async function _rdListNext(listId, bookId) {
  let nb = null;
  try { nb = (await api.get(`/api/readlists/${listId}/next?after=${bookId}`)).book_id; } catch {}
  if (!_rd.book || _rd.book.id !== bookId || _rd.list !== listId) return;
  const note = document.getElementById('rd-end-note');
  const actions = document.querySelector('.rd-end-actions');
  if (!nb) { if (note) note.textContent = 'That was the last one on the list.'; _rdSeriesNext(bookId, true); return; }
  try {
    const next = await api.get(`/api/books/${nb}`);
    if (!_rd.book || _rd.book.id !== bookId) return;
    if (note) note.textContent = '';
    actions?.insertAdjacentHTML('beforeend',
      `<button class="btn btn-primary" onclick="navigate('read', {book: ${nb}, list: ${listId}})">Next: ${esc(next.title)}${next.number != null ? ' #' + next.number : ''} ›</button>`);
  } catch { if (note) note.textContent = 'Next on the list couldn’t open.'; }
}

function _rdCenterStrip() {
  const on = document.querySelector('#rd-strip .rd-thumb.on');
  if (on) on.scrollIntoView({ block: 'nearest', inline: 'center' });
}

// --- navigation ----------------------------------------------------------------

function _rdGo(page) {
  _rd.ended = false;
  const to = _rdSpreadOf(Math.max(1, Math.min(page, _rd.book.page_count)));
  if (to !== _rd.at) _rd.turn = { dir: to > _rd.at ? 1 : -1, by: 'tap' };
  _rd.at = to;
  _rdRender();
}

function _rdStep(dir, by = 'tap') {
  if (_rd.ended) { if (dir < 0) _rdEnded(false); return; }
  const next = _rd.at + dir;
  if (next >= _rd.spreads.length) return _rdEnded(true);
  if (next < 0) return;
  _rd.at = next;
  _rd.turn = { dir, by };
  _rdRender();
}

function _rdEnded(on) {
  _rd.ended = on;
  if (!on) _rdCancelCountdown();
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

// The series this book belongs to, from inside the reader.
function _rdGoSeries() {
  const b = _rd.book;
  if (!b) return;
  if (b.series_id) navigate('series-detail', { id: b.series_id });
  else if (b.shelf_id) navigate('shelf', { id: b.shelf_id });
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
  // Opening a book is not reading it. Page 1 is never recorded — turn a page
  // and it counts — so Continue reading holds what you're IN, not what you
  // glanced at (2026-10-08: thirteen page-1 ghosts from an afternoon's poking).
  if (page <= 1 && !completed) return;
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
      _rdStep(dx < 0 ? 1 : -1, 'swipe');
    }
  });
  document.addEventListener('keydown', e => {
    if (currentView !== 'read' || !_rd.book) return;
    if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') { e.preventDefault(); _rdStep(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); _rdStep(-1); }
    else if (e.key === 'Escape') { e.preventDefault(); _rd.chrome ? _rdToggleChrome(false) : _rdExit(); }
    else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); _rdFullscreen(); }
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
    Object.assign(_rd, { book, shift: false, chrome: false, ended: false, lastSaved: null,
      list: params.list ? parseInt(params.list, 10) : null });
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

// Full screen, on whatever this is. Desktop and Android: the browser's own
// full-screen mode (the F key too). iPhone and iPad Safari refuse it for
// anything but video — there the answer is the home-screen web app, which
// runs with no browser chrome at all, so say so instead of doing nothing.
function _rdFullscreen() {
  const el = document.documentElement;
  const standalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  const req = el.requestFullscreen || el.webkitRequestFullscreen;
  if (req) {
    try { const p = req.call(el); if (p && p.catch) p.catch(() => _rdFullscreenHint(standalone)); }
    catch { _rdFullscreenHint(standalone); }
    return;
  }
  _rdFullscreenHint(standalone);
}

function _rdFullscreenHint(standalone) {
  if (standalone) { showToast('You’re already full screen — this is the home-screen app'); return; }
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  showToast(ios ? 'Safari won’t go full screen for a page. Share → Add to Home Screen, and open Kometa from there: no browser chrome at all.'
                : 'This browser won’t go full screen here.', 'error');
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
  if (_rd.list) { navigate('readlist', { id: _rd.list }); return; }
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

// One book on the shelf as a tile: cover, read tick or progress bar, label.
// `extra` rides into the reader ({list: id} keeps 'next' on the list's order).
// `ctx` names the series behind the book ({series_id} or {shelf_id}) so the
// tile's ⋯ can open the same modal the On Deck cards use.
function _bookTile(b, seriesTitle, extra = {}, ctx = {}) {
  const p = b.progress;
  const mark = p?.completed ? '<div class="shelf-tile-done" aria-label="Read">✓</div>'
    : p ? `<div class="shelf-tile-bar"><div style="width:${b.page_count ? Math.round(p.page / b.page_count * 100) : 10}%"></div></div>` : '';
  const go = `navigate('read', ${JSON.stringify({ book: b.id, ...extra }).replace(/"/g, "'")})`;
  const actions = JSON.stringify({ book_id: b.id, series: seriesTitle, label: b.label, number: b.number ?? null,
    series_id: ctx.series_id || null, shelf_id: ctx.shelf_id || null, dismissable: false,
    completed: !!(p && p.completed), page_count: b.page_count || null }).replace(/"/g, '&quot;');
  return `<div class="issue-tile${p?.completed ? ' shelf-tile-read' : ''}" id="od-${b.id}" tabindex="0" role="button" title="${esc(seriesTitle)} ${esc(b.label)}"
      onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
    <div class="issue-tile-img"><img src="/api/books/${b.id}/cover" alt="${esc(b.label)}" loading="lazy"
      onerror="this.parentElement.classList.add('unknown');this.remove()">${mark}
      <button class="od-menu" title="Actions" aria-label="Actions" onclick="event.stopPropagation(); _bookActions(${actions})">⋯</button></div>
    <div class="issue-tile-num">${esc(b.label)}</div>
  </div>`;
}

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
  const tiles = s.books.map(b => _bookTile(b, s.title, {}, { shelf_id: id, series_id: s.tracked_series_id || null })).join('');
  setApp(s.books.length
    ? `<div class="issue-grid">${tiles}</div>`
    : '<div class="state-msg">No readable books in this folder.</div>');
}

// --- Series page: matching + files on the shelf ---------------------------------

async function _loadMatchCandidates(seriesId, q) {
  const box = document.getElementById('match-results');
  if (!box) return;
  box.innerHTML = '<div class="match-hint">Searching…</div>';
  // Metron first (the API built for this), LOCG alongside when it's open to us.
  // Either can be shut; whatever answers is what you pick from.
  const enc = encodeURIComponent(q);
  const [metron, locg] = await Promise.all([
    api.get(`/api/search/metron?q=${enc}`).catch(() => []),
    api.get(`/api/search/locg?q=${enc}`).then(r => r.filter(x => !x.needs_resolve)).catch(() => []),
  ]);
  if (currentView !== 'series-detail' || currentParams.id !== seriesId) return;
  // One list: the catalogue's runs AND your own shelf. A catalogue run you
  // already have shows as yours ("File under"), not as a thing to match twice;
  // shelf series whose title matches the words show too, for the deluxe whose
  // catalogue entry Metron simply doesn't have.
  if (!_fileUnderAll) { try { _fileUnderAll = await api.get('/api/series'); } catch { _fileUnderAll = []; } }
  const mine = (_fileUnderAll || []).filter(s => s.id !== seriesId && s.kind !== 'arc' && s.folder_path
    && s.match_status !== 'pending' && s.match_status !== 'needs_match');
  const byMetron = new Map(mine.filter(s => s.metron_series_id).map(s => [s.metron_series_id, s]));
  const byLocg = new Map(mine.filter(s => s.locg_series_id).map(s => [s.locg_series_id, s]));
  const rows = metron.map(r => ({ ...r, source: 'metron', have: byMetron.get(r.id) })).slice(0, 8)
    .concat(locg.map(r => ({ ...r, source: 'locg', have: byLocg.get(r.id) })).slice(0, 8));
  _matchRows = rows;
  const words = q.toLowerCase().split(/\s+/).filter(w => w.length > 2 && !/^(the|and|of|deluxe|edition|omnibus|tpb|hc|vol)$/.test(w));
  const seenParents = new Set(rows.filter(r => r.have).map(r => r.have.id));
  const shelfHits = words.length ? mine.filter(s => !seenParents.has(s.id) && words.every(w => s.title.toLowerCase().includes(w))).slice(0, 5) : [];
  const catalogueRow = r => `
    <div class="match-row${r.have ? ' match-row-have' : ''}">
      ${r.cover ? `<img src="${esc(r.cover)}" alt="" loading="lazy">` : (r.have ? `<img src="/api/series/${r.have.id}/thumbnail" alt="" loading="lazy" onerror="this.remove()">` : '')}
      <div class="match-row-text"><div class="match-row-title">${esc(r.series)}</div>
        <div class="match-row-meta">${esc([r.publisher?.name, r.year_began, r.issue_count ? `${r.issue_count} issues` : null].filter(Boolean).join(' · '))}
          <span class="match-source">${r.source === 'metron' ? 'Metron' : 'LOCG'}</span>
          ${r.have ? `<span class="match-have">· on your shelf as ${esc(r.have.title)}</span>` : ''}</div></div>
      ${r.have
        ? `<button class="btn btn-primary btn-sm" onclick="_fileUnder(${seriesId}, ${r.have.id})">File under</button>`
        : `<button class="btn btn-primary btn-sm" onclick="_pickMatch(${seriesId}, ${r.id}, this, '${r.source}')">Use this</button>`}
    </div>`;
  const shelfRow = s => `
    <div class="match-row match-row-have">
      <img src="/api/series/${s.id}/thumbnail" alt="" loading="lazy" onerror="this.remove()">
      <div class="match-row-text"><div class="match-row-title">${esc(s.title)}</div>
        <div class="match-row-meta">${esc([s.publisher, s.year_began, `${s.owned ?? 0} owned`].filter(Boolean).join(' · '))}
          <span class="match-have">· on your shelf</span></div></div>
      <button class="btn btn-ghost btn-sm" title="Move this file into that series as its next issue, keeping its title" onclick="_addAsIssue(${seriesId}, ${s.id})">Add as issue</button>
      <button class="btn btn-primary btn-sm" onclick="_fileUnder(${seriesId}, ${s.id})">File under</button>
    </div>`;
  box.innerHTML = (rows.length || shelfHits.length)
    ? rows.map(catalogueRow).join('') + shelfHits.map(shelfRow).join('')
    : '<div class="match-hint">No series found — try a different search.</div>';
}

// You picked a run by hand: say back what you're about to link, next to what's
// actually in the folder, and flag the obvious mismatches (more files than the
// run has issues; a one-shot for a 40-file folder) BEFORE anything is written.
// A wrong link brings the wrong issue list and the wrong covers — cheap to
// stop here, annoying to notice later.
let _matchRows = [];
async function _pickMatch(seriesId, runId, btn, source = 'locg') {
  const r = (_matchRows || []).find(x => x.id === runId && x.source === source) || { id: runId, source };
  // Already have this run as a series? Then this folder is a second copy of it
  // — a deluxe, a trade, a split — and matching would mint a duplicate series
  // with the collection reading as issue #1. Offer to file it under the one you
  // have instead. (Year 100 and Other Tales Deluxe → Batman - Year 100.)
  if (!_fileUnderAll) { try { _fileUnderAll = await api.get('/api/series'); } catch {} }
  const have = (_fileUnderAll || []).find(s => s.id !== seriesId && s.folder_path
    && (source === 'metron' ? s.metron_series_id === runId : s.locg_series_id === runId));
  if (have) { _fileUnder(seriesId, have.id, r); return; }
  const s = _detailSeries && _detailSeries.id === seriesId ? _detailSeries : null;
  // Count FILES on the shelf, not owned issues: a trade or a one-file folder
  // with no issue number is 0 issues and 1 file, and '0 files' is a lie.
  let files = s ? (s.issues || []).filter(i => i.owned).length : null;
  if (s && s.shelf_id) {
    try { files = (await api.get(`/api/shelf/${s.shelf_id}`)).books.length; } catch {}
  }
  const warns = [];
  if (files != null && r.issue_count != null && files > r.issue_count)
    warns.push(`Your folder has ${files} files; this run has ${r.issue_count} issues.`);
  if (s && s.publisher && r.publisher?.name && s.publisher.toLowerCase() !== r.publisher.name.toLowerCase())
    warns.push(`Folder is filed under ${s.publisher}; this run is ${r.publisher.name}.`);
  showModal(`
    <div class="modal-header"><h2>Match this run?</h2></div>
    <div class="modal-body">
      <div style="font-weight:600">${esc(r.series || 'Run #' + runId)}</div>
      <div class="u-label" style="color:var(--tq);margin-top:4px">${esc([r.publisher?.name, r.year_began, r.issue_count ? `${r.issue_count} issues` : null, source === 'metron' ? 'Metron' : 'LOCG'].filter(Boolean).join(' · '))}</div>
      ${s ? `<div class="u-label" style="color:var(--tq);margin-top:10px">Your folder: ${esc(s.title)}${s.publisher ? ' · ' + esc(s.publisher) : ''}${files != null ? ` · ${files} file${files === 1 ? '' : 's'}` : ''}</div>` : ''}
      ${warns.length ? `<div style="margin-top:10px;color:var(--amb);font-size:13px">${warns.map(esc).join('<br>')}</div>` : ''}
      <div style="margin-top:10px;color:var(--tq);font-size:12px">The folder and files stay as they are. Matching gives this series its issue list, details and covers.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="link-run-btn">Match</button>
    </div>`);
  document.getElementById('link-run-btn').onclick = async (ev) => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = 'Matching…';
    try {
      await api.patch(`/api/series/${seriesId}/locg`, source === 'metron' ? { metron_id: runId } : { locg_id: runId });
      closeModal();
      showToast(`Matched to ${r.series || 'the run'} — fetching issues and covers`);
      _awaitSync(seriesId);
    } catch (e) {
      closeModal(); showToast('Couldn’t match that run', 'error');
    }
  };
}

// "File it under a run you already have": a deluxe / omnibus / TPB that became
// its own series by accident. Search your OWN shelf (client-side, it's one
// list), confirm, and the file moves into the parent's folder — readable from
// the parent's page, the stub series gone.
let _fileUnderAll = null;

async function _fileUnder(seriesId, parentId, run = null, opts = {}) {
  if (!_fileUnderAll) { try { _fileUnderAll = await api.get('/api/series'); } catch { _fileUnderAll = []; } }
  const parent = (_fileUnderAll || []).find(s => s.id === parentId) || { title: 'that series' };
  const me = (_detailSeries && _detailSeries.id === seriesId) ? _detailSeries
    : (_fileUnderAll || []).find(s => s.id === seriesId) || { title: 'this' };
  showModal(`
    <div class="modal-header"><h2>File under ${esc(parent.title)}?</h2></div>
    <div class="modal-body">
      ${run ? `<div style="margin-bottom:8px">You already have <b>${esc(run.series || parent.title)}</b> on your shelf as <b>${esc(parent.title)}</b>. Matching this folder to it as well would make a second series of the same run.</div>` : ''}
      <div><b>${esc(me.title)}</b> is treated as a collected edition of <b>${esc(parent.title)}</b>.</div>
      <div style="margin-top:10px;color:var(--tq);font-size:12px">Its file moves into that series' folder, named as a collection so it can never be mistaken for an issue. This series and its empty folder go away. Reading progress follows the file.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="file-under-btn">File it</button>
    </div>`);
  document.getElementById('file-under-btn').onclick = async (ev) => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = 'Moving…';
    try {
      // from the report, go through the filing pass so the wiki's year lands on the file name
      const [url, body] = opts.stay
        ? ['/api/report/collections/apply', { series_id: seriesId, parent_id: parentId }]
        : [`/api/series/${seriesId}/file-under`, { parent_id: parentId }];
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const r = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(r.detail || res.status);
      closeModal();
      showToast(`Filed under ${r.parent_title}: ${r.files.join(', ')}`);
      _fileUnderAll = null;
      if (opts.stay) {
        const row = document.getElementById(`sg-${seriesId}`);
        if (row) { row.style.transition = 'opacity .3s'; row.style.opacity = '0'; setTimeout(() => row.remove(), 320); }
        return;
      }
      navigate('series-detail', { id: r.parent_id });
    } catch (e) {
      closeModal(); showToast(`Couldn’t file it: ${e.message || ''}`, 'error');
    }
  };
}

// "No run": you looked, and there's nothing to pick — an omnibus library, a
// folder of yearly specials, a fan comic. It stays a shelf series (readable,
// no issue list) and leaves Needs matching for good.
async function _noRun(seriesId) {
  const me = _detailSeries && _detailSeries.id === seriesId ? _detailSeries : { title: 'this series' };
  showModal(`
    <div class="modal-header"><h2>No run to match?</h2></div>
    <div class="modal-body">
      <div><b>${esc(me.title)}</b> stays on the shelf as it is: readable, with its files, but no issue list, covers or pull list.</div>
      <div style="margin-top:10px;color:var(--tq);font-size:12px">Right for omnibus libraries, folders of one-shots and specials, fan books. You can still pick a run later from its page.</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="no-run-btn">Keep as is</button>
    </div>`);
  document.getElementById('no-run-btn').onclick = async () => {
    try {
      await api.patch(`/api/series/${seriesId}/locg`, { none: true });
      closeModal(); showToast('Kept as a shelf series');
      if (currentView === 'needs-match') {
        const row = document.getElementById(`nm-${seriesId}`); if (row) row.remove();
        _nmRows = (_nmRows || []).filter(x => x.id !== seriesId); _updateNeedsBadge(_nmRows.length);
      } else renderSeriesDetail(seriesId);
    } catch (e) { closeModal(); showToast('Couldn’t save that', 'error'); }
  };
}

// "Add as issue": this one-file series becomes the next issue of a series you
// have — 'Tintin in Thailand' → 'The Adventures of Tintin #25 - Tintin in
// Thailand'. Number defaults to the series' highest + 1; change it if the book
// belongs elsewhere in the order (Combine's numbering is yours to fix later).
async function _addAsIssue(seriesId, targetId) {
  if (!_fileUnderAll) { try { _fileUnderAll = await api.get('/api/series'); } catch { _fileUnderAll = []; } }
  const target = (_fileUnderAll || []).find(s => s.id === targetId) || { title: 'that series' };
  const me = (_detailSeries && _detailSeries.id === seriesId) ? _detailSeries : (_fileUnderAll || []).find(s => s.id === seriesId) || { title: 'this' };
  let next = 1;
  try { const t = await api.get(`/api/series/${targetId}`); next = Math.max(0, ...t.issues.map(i => i.number || 0)) + 1; } catch {}
  const stem = target.title.replace(/\s*\(\d{4}\)\s*$/, '');
  const sub = me.title.replace(/\s*\(\d{4}\)\s*$/, '');
  const preview = n => `${esc(stem)} #${String(n).padStart(2, '0')} - ${esc(sub)}`;
  showModal(`
    <div class="modal-header"><h2>Add to ${esc(stem)}?</h2></div>
    <div class="modal-body"><b>${preview(next)}</b></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="add-issue-btn">Add</button>
    </div>`);
  document.getElementById('add-issue-btn').onclick = async (ev) => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = 'Moving…';
    const number = next;
    try {
      const res = await fetch('/api/report/combine/add', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [seriesId], into: targetId, number }) });
      const r = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(r.detail || res.status);
      closeModal();
      showToast(`Added to ${r.title} as #${r.numbers[0]}`);
      _fileUnderAll = null;
      navigate('series-detail', { id: r.series_id });
    } catch (e) { closeModal(); showToast(`Couldn’t add it: ${e.message || ''}`, 'error'); }
  };
}

// After a match or link the sync runs in the background. Re-render the page when
// it lands (last_synced moves) rather than leaving you on a page painted before
// the issues had their details — up to a minute, then give up quietly.
async function _awaitSync(seriesId) {
  let before = null;
  try { before = (await api.get(`/api/series/${seriesId}`)).last_synced || null; } catch {}
  renderSeriesDetail(seriesId);
  const stop = Date.now() + 60000;
  const tick = async () => {
    if (currentView !== 'series-detail' || currentParams.id !== seriesId) return;
    let fresh = null;
    try { fresh = await api.get(`/api/series/${seriesId}`); } catch {}
    if (fresh && fresh.last_synced && fresh.last_synced !== before) {
      renderSeriesDetail(seriesId);
      showToast('Issues and covers are in');
      return;
    }
    if (Date.now() < stop) setTimeout(tick, 2500);
  };
  setTimeout(tick, 2500);
}

// Files on the shelf that aren't numbered issues (trades, omnibuses, oddities) —
// or, for a series with no issue list at all, every file — so nothing on disk is
// unreadable just because it doesn't fit the issue grid.
async function _loadShelfFiles(s, all) {
  let shelf;
  try { shelf = await api.get(`/api/shelf/${s.shelf_id}`); } catch { return; }
  if (currentView !== 'series-detail' || currentParams.id !== s.id) return;
  const books = all ? shelf.books : shelf.books.filter(b => b.number == null && !b.trade);   // trades live on the Trades tab
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
    const m = body.matched;
    const who = m ? `${m.title}${m.publisher ? ' · ' + m.publisher : ''}${m.year ? ' · ' + m.year : ''} (${m.source === 'metron' ? 'Metron' : 'LOCG'})` : '';
    if (body.match_status === 'auto') {
      showToast(`Matched to ${who} — fetching issues and covers`);
      if (currentView === 'needs-match') {
        // the row's done its job: fade it, keep the count honest, stay on the list
        const row = document.getElementById(`nm-${seriesId}`);
        if (row) { row.style.transition = 'opacity .3s'; row.style.opacity = '0'; setTimeout(() => row.remove(), 320); }
        _updateNeedsBadge(Math.max(0, document.querySelectorAll('.nm-row').length - 1));
      } else {
        _awaitSync(seriesId);
      }
    } else {
      showToast(currentView === 'needs-match' ? 'No single run fits — open it and pick' : 'More than one run could fit — pick it below');
      if (currentView === 'needs-match') navigate('series-detail', { id: seriesId });
      else renderSeriesDetail(seriesId);
    }
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
  // Nothing is disabled: matching runs through Metron and doesn't care that LOCG
  // is shut. (It used to grey out Match now — with the one source that worked.)
  if (!banner.querySelector('.match-paused')) {
    banner.insertAdjacentHTML('beforeend', `<div class="match-paused">LOCG paused until ${esc(st.paused_until_label)} · Metron still answers
      <button class="btn-link" onclick="_testLocg(this)">Test LOCG now</button></div>`);
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


// --- Reading lists -------------------------------------------------------------
// An order to read in, across series (kometa/readlists.py). CBL files in — the
// community repo on GitHub holds ~1,700 — resolved against the shelf every time
// the page opens, so a Combine or a new download shows up without a re-import.

async function renderReadLists() {
  setTopbar();
  document.getElementById('topbar-title').textContent = 'Reading lists';
  document.getElementById('topbar-actions').innerHTML =
    `<button class="btn btn-primary btn-sm" onclick="_rlImportModal()">+ Import</button>`;
  setApp('<div class="state-msg">Loading...</div>');
  const lists = await api.get('/api/readlists');
  if (currentView !== 'readlists') return;
  if (!lists.length) {
    setApp(`<div class="empty-state"><div class="empty-state-title">No reading lists yet</div>
      <div style="margin-top:8px;color:var(--tq);font-size:13px">Import a ComicRack .cbl, a link to one, or Komga's read lists.</div></div>`);
    return;
  }
  // The Library's cards: cover, an owned bar, title and count. Here the bar is
  // how much of the list is on the shelf; lime when all of it is.
  const cards = lists.map((l, i) => {
    // The card: the bar is reading progress in books you have; the corner count is
    // entries on the shelf; an amber line says how many entries aren't here.
    const total = l.total || l.entries || 0, owned = l.owned || 0;
    const books = l.books || 0, read = l.books_read || 0, gaps = total - owned;
    const pct = books ? Math.round(read / books * 100) : 0;
    const color = gaps ? 'var(--amb)' : (total ? 'var(--pri)' : 'var(--tq)');
    const go = `navigate('readlist', {id: ${l.id}})`;
    const coverSrc = l.cover_book_id ? `/api/books/${l.cover_book_id}/cover` : (l.cover ? _ext(l.cover) : '');
    const cover = coverSrc ? `<img class="series-card-cover" src="${esc(coverSrc)}" alt="" loading="lazy" onerror="this.style.opacity='0.15'">`
      : `<div class="series-card-cover rl-nocover"></div>`;
    const sub = [books ? `${pct}% READ` : '', gaps ? `<span style="color:var(--amb)">${gaps} MISSING</span>` : ''].filter(Boolean).join(' · ');
    return `
      <div class="series-card card-cascade" style="animation-delay:${Math.min(i, 14) * STAGGER_MS}ms" tabindex="0" role="button"
        onclick="${go}" onkeydown="if(event.key==='Enter'||event.key===' ')${go}">
        <div class="series-card-img-wrap">${cover}</div>
        <div class="series-card-bar-track"><div class="series-card-bar-fill" style="width:${pct}%;background:${books && read === books ? 'var(--pri)' : 'var(--pri)'}"></div></div>
        <div class="series-card-footer">
          <div class="series-card-title">${esc(l.name)}</div>
          <div class="series-card-count" style="color:${color}">${owned}/${total}</div>
        </div>
        <div class="series-card-publisher u-truncate">${sub}</div>
      </div>`;
  }).join('');
  setApp(`<div class="series-grid">${cards}</div>`);
}

// One modal, three ways in: a .cbl file, a link to one, or Komga's read lists.
function _rlImportModal() {
  showModal(`
    <div class="modal-header"><h2>Import a reading list</h2></div>
    <div class="modal-body rl-import">
      <label class="btn btn-ghost rl-file">Choose a .cbl file<input type="file" accept=".cbl,.xml,text/xml" onchange="_rlImportFile(this)" hidden></label>
      <div class="settings-field" style="margin-top:14px"><label class="settings-field-label u-label" for="rl-url">Or a link to one</label>
        <div style="display:flex;gap:8px"><input class="settings-input" id="rl-url" type="url" placeholder="https://github.com/DieselTech/CBL-ReadingLists/…" autocomplete="off" spellcheck="false"
          onkeydown="if(event.key==='Enter')_rlImportUrl()" style="flex:1;min-width:0">
        <button class="btn btn-ghost" onclick="_rlImportUrl()">Import</button></div>
        <div style="margin-top:6px;color:var(--tq);font-size:12px"><a class="btn-link" href="https://github.com/DieselTech/CBL-ReadingLists" target="_blank" rel="noopener">The community repo</a> has about 1,700.</div></div>
      <div class="settings-field" style="margin-top:14px"><label class="settings-field-label u-label">Or from Komga</label>
        <button class="btn btn-ghost" title="Every read list in Komga, matched by file name" onclick="_rlImportKomga(this)">Import Komga's read lists</button></div>
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div>`);
}

async function _rlImportFile(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const text = await f.text();
  await _rlImport(fetch(`/api/readlists/import?name=${encodeURIComponent(f.name)}`,
    { method: 'POST', headers: { 'Content-Type': 'application/xml' }, body: text }));
}

async function _rlImportUrl() {
  const url = (document.getElementById('rl-url')?.value || '').trim();
  if (!url) return;
  await _rlImport(fetch('/api/readlists/import-url', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }));
}

async function _rlImport(pending) {
  try {
    const res = await pending;
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.detail || res.status);
    closeModal();
    showToast('List imported');
    navigate('readlist', { id: r.id });
  } catch (e) { showToast(`Couldn’t import that: ${e.message || e}`, 'error'); }
}

async function _rlImportKomga(btn) {
  btn.disabled = true; btn.textContent = 'Importing…';
  try {
    const made = await api.post('/api/readlists/import-komga', {});
    closeModal();
    showToast(made.length ? `${made.length} list${made.length === 1 ? '' : 's'} imported from Komga` : 'Komga has no read lists');
    renderReadLists();
  } catch (e) { btn.disabled = false; btn.textContent = 'Import from Komga'; showToast('Couldn’t import from Komga', 'error'); }
}

function _rlDelete(id, name) {
  showModal(`
    <div class="modal-header"><h2>Remove ${esc(name)}?</h2></div>
    <div class="modal-body">The list goes. Your books and reading progress stay.</div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="rl-del-btn">Remove</button>
    </div>`);
  document.getElementById('rl-del-btn').onclick = async () => {
    try { await api.del(`/api/readlists/${id}`); closeModal(); navigate('readlists'); }
    catch (e) { closeModal(); showToast('Couldn’t remove it', 'error'); }
  };
}

let _rlTab = 'all';
function setRlTab(tab, id) { _rlTab = tab; renderReadList(id); }

// The series page's layout: a cover for the backdrop, a count chip, a sub line,
// tabs, and one grid of tiles in reading order. A gap in the order is a tile
// too — amber frame, no cover — so the list reads as the list, holes and all.
// 'Continue' only when you've read something AT or BEFORE the book the button
// opens — a half-read Infinity #2 from before the list existed is not you
// being under way with Books of Doom #1.
function _rlStarted(l) {
  for (const e of l.entries) for (const b of e.books) {
    if (b.progress) return true;
    if (b.id === l.continue) return false;
  }
  return false;
}

async function renderReadList(id) {
  setTopbar();
  setApp('<div class="state-msg">Loading...</div>');
  const l = await api.get(`/api/readlists/${id}`);
  if (currentView !== 'readlist' || currentParams.id !== id) return;
  const books = l.entries.flatMap(e => e.books);
  const bg = document.getElementById('series-bg'), bgImg = document.getElementById('series-bg-img');
  if (books.length) {
    bgImg.style.backgroundImage = `url("/api/books/${books[Math.floor(Math.random() * books.length)].id}/cover")`;
    bg.classList.remove('hidden');
  }
  const gaps = l.total - l.owned;
  document.getElementById('topbar-title').textContent = l.name;
  // Chips, each one number: HAVE and MISSING in entries, READ and READING in
  // books you have. Lime when a count is complete, amber while something is
  // open, pink for a gap. A chip that would read zero isn't shown.
  const done = l.books > 0 && l.books_read === l.books;
  document.getElementById('topbar-chips').innerHTML = [
    `<span class="chip ${gaps ? 'chip-neutral' : 'chip-complete'}" title="entries on the shelf">HAVE ${l.owned}/${l.total}</span>`,
    gaps ? `<span class="chip chip-missing" title="entries not on the shelf">${gaps} MISSING</span>` : '',
    l.books ? `<span class="chip ${done ? 'chip-complete' : 'chip-neutral'}" title="books read, of the ones you have">READ ${l.books_read}/${l.books}</span>` : '',
    l.books_reading ? `<span class="chip chip-reading" title="books started">${l.books_reading} READING</span>` : '',
  ].filter(Boolean).join('');
  document.getElementById('topbar-sub').innerHTML = `<span class="u-label" style="color:var(--tq)">
    ${l.source === 'komga' ? 'FROM KOMGA' : 'CBL'} · ${l.total} ENTR${l.total === 1 ? 'Y' : 'IES'} · ${books.length} BOOK${books.length === 1 ? '' : 'S'} ON THE SHELF</span>`;
  document.getElementById('topbar-actions').innerHTML = `
    ${gaps ? `<button class="btn btn-ghost btn-sm" id="rl-getmissing" onclick="_rlGetMissing(${id}, ${gaps})">Get missing (${gaps})</button>` : ''}
    ${l.continue ? `<button class="btn btn-primary btn-sm" onclick="navigate('read', {book: ${l.continue}, list: ${id}})">${_rlStarted(l) ? 'Continue' : 'Start'}</button>`
      : (done && books.length ? `<button class="btn btn-primary btn-sm" onclick="navigate('read', {book: ${books[0].id}, list: ${id}})">Read again</button>` : '')}
    <button class="btn btn-ghost btn-sm" onclick="_rlDelete(${id}, ${JSON.stringify(l.name).replace(/"/g, '&quot;')})">Remove</button>`;
  api.get(`/api/readlists/${id}/get-missing`).then(st => { if (st && st.running) _rlPollGetMissing(id); }).catch(() => {});
  const tabs = ['all', 'on shelf', 'not here'].map(t => `<div class="issue-tab ${_rlTab === t ? 'active' : ''}" tabindex="0" role="tab"
      aria-selected="${_rlTab === t}" onclick="setRlTab('${t}', ${id})" onkeydown="if(event.key==='Enter'||event.key===' ')setRlTab('${t}', ${id})">${t}</div>`).join('');
  const tiles = [];
  for (const e of l.entries) {
    const owned = e.status === 'owned';
    if (_rlTab === 'on shelf' && !owned) continue;
    if (_rlTab === 'not here' && owned) continue;
    if (owned) {
      for (const b of e.books) tiles.push(_bookTile({ ...b, label: `${e.series} ${b.label}` }, e.series, { list: id }, { series_id: e.series_id, shelf_id: e.shelf_id }));
    } else {
      const label = `${e.series}${e.number ? ' #' + e.number : ''}`;
      const go = e.series_id ? `navigate('series-detail', {id: ${e.series_id}})` : (e.shelf_id ? `navigate('shelf', {id: ${e.shelf_id}})` : '');
      tiles.push(`<div class="issue-tile rl-gap" id="rl-item-${e.item_id}" ${go ? `tabindex="0" role="button" onclick="${go}"` : ''} title="${esc(label)} — ${e.status === 'missing' ? 'not here yet' : 'not on the shelf'}">
        <div class="issue-tile-img missing${e.cover ? '' : ' unknown'}">${e.cover ? `<img src="${esc(e.cover)}" alt="" loading="lazy" onerror="this.parentElement.classList.add('unknown');this.remove()">` : ''}
          <button class="od-menu" title="Actions" aria-label="Actions" onclick="event.stopPropagation(); _rlGapActions(${id}, ${e.item_id}, ${JSON.stringify(label).replace(/"/g, '&quot;')}, ${e.series_id || 'null'}, ${e.shelf_id || 'null'})">⋯</button></div>
        <div class="issue-tile-num">${esc(label)}</div></div>`);
    }
  }
  setApp(`
    ${_rlProgressHtml(l)}
    <div class="issue-tabs-row"><div class="issue-tabs">${tabs}</div></div>
    ${tiles.length ? `<div class="issue-grid rl-grid">${tiles.join('')}</div>` : '<div class="state-msg">Nothing here.</div>'}`);
  // Gaps without a cover yet: ask the catalogue (ComicVine in one go, Metron a
  // few at a time) and repaint when something landed. Each answer is cached on
  // the list, so this runs down to nothing after the first few opens.
  if (l.entries.some(e => (e.status !== 'owned' || e.via_run) && e.cover_pending)) _rlFillCovers(id);
}

// How far through the list you are: books read of the books you have, the
// share of the whole list read, and what the Start/Continue button opens next.
function _rlProgressHtml(l) {
  const books = l.entries.flatMap(e => e.books.map(b => ({ ...b, series: e.series, position: e.position })));
  if (!books.length) return '';
  const read = books.filter(b => b.progress && b.progress.completed).length;
  const pct = Math.round(read / books.length * 100);
  const next = books.find(b => b.id === l.continue);
  const label = read === books.length ? 'Finished' : next
    ? `${_rlStarted(l) ? 'Next' : 'Starts with'}: ${esc(next.series)} ${esc(next.label || '')} · #${next.position} of ${l.total}` : '';
  return `<div class="rl-progress">
    <div class="rl-progress-text"><span class="rl-progress-pct">${pct}%</span>
      <span>${read} of ${books.length} read</span>${label ? `<span class="rl-progress-next">${label}</span>` : ''}</div>
    <div class="rl-progress-track"><div class="rl-progress-fill" style="transform:scaleX(${read / books.length})"></div></div>
  </div>`;
}

async function _rlFillCovers(id) {
  try {
    const r = await api.post(`/api/readlists/${id}/covers`, {});
    if (currentView === 'readlist' && currentParams.id === id && r.filled) renderReadList(id);
  } catch {}
}


// A gap tile's ⋯: Get it, or go to the run if part of it is on the shelf.
function _rlGapActions(listId, itemId, label, seriesId, shelfId) {
  showModal(`
    <div class="modal-header"><h2>${esc(label)}</h2></div>
    <div class="modal-body action-sheet">
      <button class="sheet-btn" onclick="closeModal(); _rlGet(${listId}, ${itemId}, document.getElementById('rl-item-${itemId}')?.querySelector('.od-menu'))">Get — track the run, fetch what this entry covers</button>
      ${seriesId ? `<button class="sheet-btn" onclick="closeModal(); navigate('series-detail', {id: ${seriesId}})">Go to series</button>`
        : shelfId ? `<button class="sheet-btn" onclick="closeModal(); navigate('shelf', {id: ${shelfId}})">Go to series</button>` : ''}
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button></div>`);
}

// --- Get: an entry you don't have becomes an acquisition --------------------------
// One entry: track the run (pull list off) if it isn't tracked, queue what the
// entry covers. The tile reports the outcome in place; a run no catalogue knows
// says so rather than pretending.
async function _rlGet(listId, itemId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await api.post(`/api/readlists/${listId}/items/${itemId}/get`, {});
    if (r.result === 'queued') {
      showToast(`${r.series_title}: ${r.queued} queued${r.created ? ', now tracked' : ''}${r.upcoming ? `, ${r.upcoming} not out yet` : ''}`);
      if (btn) { btn.textContent = '✓'; btn.classList.add('on'); }
    } else if (r.result === 'owned') { if (btn) btn.textContent = '✓'; }
    else { showToast(r.detail || 'No catalogue knows that run', 'error'); if (btn) { btn.textContent = '?'; btn.classList.add('off'); } }
  } catch (e) { if (btn) { btn.disabled = false; btn.textContent = '⋯'; } showToast('Couldn’t get that', 'error'); }
}

// Every gap on the list, in the background; the top-bar button shows progress.
function _rlGetMissing(listId, count) {
  showModal(`
    <div class="modal-header"><h2>Get ${count} missing?</h2></div>
    <div class="modal-body">Each entry's run is tracked without being put on the pull list, and only the issues the list covers are fetched.
      Runs no catalogue knows are listed afterwards.</div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="rl-gm-btn">Get missing</button>
    </div>`);
  document.getElementById('rl-gm-btn').onclick = async () => {
    closeModal();
    try { await api.post(`/api/readlists/${listId}/get-missing`, {}); } catch { showToast('Couldn’t start', 'error'); return; }
    _rlPollGetMissing(listId);
  };
}

async function _rlPollGetMissing(listId) {
  const btn = document.getElementById('rl-getmissing');
  let st;
  try { st = await api.get(`/api/readlists/${listId}/get-missing`); } catch { return; }
  if (btn) {
    btn.disabled = st.running;
    btn.textContent = !st.running ? `Get missing (${Math.max(0, st.total - st.done)})`
      : (st.phase === 'packs' || st.phase === 'starting') ? 'Looking for packs…' : `Getting ${st.done}/${st.total}…`;
  }
  if (st.running) { setTimeout(() => _rlPollGetMissing(listId), 2500); return; }
  const unknown = (st.unknown || []).length;
  showToast(`${st.queued} issues queued across ${st.created} new runs${unknown ? ` · ${unknown} unknown to every catalogue` : ''}`);
  if (currentView === 'readlist' && currentParams.id === listId) renderReadList(listId);
}
