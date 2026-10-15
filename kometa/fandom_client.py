import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from kometa.naming import norm_key
logger = logging.getLogger(__name__)
USER_AGENT = 'Kometa/1.0 (personal pull-list tool)'
MIN_INTERVAL_S = 1.0
WIKIS = {'dc': 'dc.fandom.com', 'marvel': 'marvel.fandom.com'}
_COLLECTED_MODES = {'tpb', 'hc', 'hardcover', 'trade paperback', 'omnibus', 'graphic novel'}
_lock = threading.Lock()
_last = {'t': 0.0, 'day': '', 'count': 0, 'paused_until': 0.0}
DAILY_CAP = 3000
REFUSED_PAUSE_S = 3600

class FandomUnavailable(RuntimeError):
    pass

def wiki_for(publisher: str | None) -> str | None:
    p = (publisher or '').lower()
    if 'dc' in p.split() or p.startswith('dc') or 'vertigo' in p or ('black label' in p) or ('wildstorm' in p):
        return 'dc'
    if 'marvel' in p:
        return 'marvel'
    return None

def _health(outcome: str, detail: str | None=None):
    from kometa import health
    health.record('Fandom wikis', outcome, detail)

def _get(host: str, **params) -> dict:
    url = f'https://{host}/api.php?' + urllib.parse.urlencode({**params, 'format': 'json'})
    with _lock:
        today = time.strftime('%Y-%m-%d')
        if _last['day'] != today:
            _last.update(day=today, count=0)
        if _last['paused_until'] > time.time():
            raise FandomUnavailable(f'{host}: paused after a refusal')
        if _last['count'] >= DAILY_CAP:
            raise FandomUnavailable(f"{host}: today's requests are spent")
        wait = _last['t'] + MIN_INTERVAL_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _last['t'] = time.time()
        _last['count'] += 1
        headers = {'User-Agent': USER_AGENT}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as r:
                body = json.load(r)
            _health('ok')
            return body
        except urllib.error.HTTPError as e:
            if e.code in (403, 503):
                try:
                    from kometa import flaresolverr
                    if flaresolverr.enabled():
                        cleared = flaresolverr.headers_for(url, headers)
                        if cleared:
                            with urllib.request.urlopen(urllib.request.Request(url, headers=cleared), timeout=25) as r:
                                body = json.load(r)
                            _health('ok')
                            return body
                except Exception:
                    try:
                        from kometa import flaresolverr
                        flaresolverr.forget(url)
                    except Exception:
                        pass
            if e.code in (403, 429, 503):
                _last['paused_until'] = time.time() + REFUSED_PAUSE_S
            _health('ok' if e.code == 404 else 'refused' if e.code in (403, 429) else 'error', str(e.code))
            raise FandomUnavailable(f'{host}: {e}') from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            _health('error', e.__class__.__name__)
            raise FandomUnavailable(f'{host}: {e}') from e

def budget() -> dict:
    today = time.strftime('%Y-%m-%d')
    used = _last['count'] if _last['day'] == today else 0
    return {'used': used, 'cap': DAILY_CAP, 'left': max(0, DAILY_CAP - used), 'paused': _last['paused_until'] > time.time()}

def search(wiki: str, query: str, limit: int=8) -> list[str]:
    d = _get(WIKIS[wiki], action='query', list='search', srsearch=query, srnamespace=0, srlimit=limit)
    return [h['title'] for h in d.get('query', {}).get('search', [])]

def wikitext(wiki: str, page: str) -> str | None:
    d = _get(WIKIS[wiki], action='parse', page=page, prop='wikitext')
    return d.get('parse', {}).get('wikitext', {}).get('*') if 'parse' in d else None
_ENTRY_RE = re.compile('^(?P<series>.+?)\\s+Vol\\s+(?P<vol>\\d+)\\s+(?P<num>[\\d.]+(?:\\.NOW)?)\\s*$', re.I)
_HASH_RE = re.compile('^(?P<series>.+?)\\s+#\\s*(?P<num>[\\d.]+)\\s*$')

def parse_entry(text: str) -> dict | None:
    t = text.strip().split('|')[0].strip()
    m = _ENTRY_RE.match(t) or _HASH_RE.match(t)
    if not m:
        t = re.sub('\\s*\\(.*?\\)\\s*$', '', t).strip()
        return {'series': t, 'volume': 1, 'number': 1.0} if t and (not t.lower().startswith(('category:', 'file:'))) else None
    num = m.group('num').upper().replace('.NOW', '')
    try:
        number = float(num)
    except ValueError:
        return None
    return {'series': m.group('series').strip(), 'volume': int(m.groupdict().get('vol') or 1), 'number': number}

def _field(w: str, key: str) -> str | None:
    m = re.search('^\\|\\s*' + re.escape(key) + '\\s*=\\s*([^\\n]*)', w, re.M)
    return m.group(1).strip() or None if m else None

def parse_collection(w: str, wiki: str) -> dict | None:
    year = _field(w, 'Year')
    isbn = _field(w, 'ISBN')
    collects = []
    if wiki == 'dc':
        m = re.search('^\\|\\s*IssueList\\s*=(.*?)(?=^\\|\\s*\\w+\\s*=|\\Z)', w, re.M | re.S)
        block = m.group(1) if m else w
        for m in re.finditer('\\{\\{c\\|([^}]+)\\}\\}|\\[\\[([^\\]|]+)(?:\\|[^\\]]*)?\\]\\]', block):
            p = parse_entry(m.group(1) or m.group(2))
            if p:
                collects.append(p)
        mode = 'collected'
    else:
        mode = (_field(w, 'Mode') or '').lower()
        if mode not in _COLLECTED_MODES:
            return None
        for m in re.finditer('^\\|\\s*ReprintOf\\d+\\s*=\\s*([^\\n]+)', w, re.M):
            p = parse_entry(m.group(1))
            if p:
                collects.append(p)
    if not collects:
        return None
    return {'year': int(year) if year and year.isdigit() else None, 'isbn': isbn, 'collects': collects, 'mode': mode}
_EDITION_WORDS = re.compile('\\b(the|deluxe|edition|omnibus|tpb|hc|hardcover|collected|collection|vol|volume|book|new|anniversary|\\d+th)\\b', re.I)
_FORMAT_WORDS = re.compile('\\b(deluxe|omnibus|absolute|compendium|compact)\\b', re.I)
MIN_SCORE = 0.6

def _core(s: str) -> set[str]:
    return set(norm_key(_EDITION_WORDS.sub(' ', re.sub('\\(Collected\\)', '', s))).split())

def _score(q: str, page: str) -> float:
    a, b = (_core(q), _core(page))
    if not a or not b or (not a <= b):
        return 0.0
    j = len(a & b) / len(a | b)
    fq = {w.lower() for w in _FORMAT_WORDS.findall(q)}
    fp = {w.lower() for w in _FORMAT_WORDS.findall(page)}
    return j + (0.05 if fq and fq <= fp else 0.0)

def lookup_collection(title: str, publisher: str | None) -> dict | None:
    wiki = wiki_for(publisher)
    if not wiki:
        return None
    q = re.sub('\\s*-\\s+', ': ', re.sub('\\(\\d{4}\\)', '', title)).strip()
    core_q = ' '.join(sorted(_core(q), key=q.lower().find))
    hits, seen = ([], set())
    for sq in (q, core_q):
        if not sq or sq.lower() in seen:
            continue
        seen.add(sq.lower())
        for h in search(wiki, f'{sq} Collected' if wiki == 'dc' else sq):
            if h not in hits and (wiki != 'dc' or h.endswith('(Collected)')):
                hits.append(h)
    ranked = sorted(((_score(q, h), h) for h in hits), key=lambda x: (-x[0], len(x[1])))
    best = ranked[0][0] if ranked else 0.0
    for score, page in ranked[:3]:
        if score < MIN_SCORE or score < best - 0.1:
            break
        w = wikitext(wiki, page)
        if not w:
            continue
        parsed = parse_collection(w, wiki)
        if parsed:
            return {'wiki': wiki, 'page': page, 'match': round(score, 2), 'url': f"https://{WIKIS[wiki]}/wiki/{urllib.parse.quote(page.replace(' ', '_'))}", **parsed}
    return None
GALLERY_MIN_WIDTH = 400

def page_title(series_title: str, volume: int | None, number) -> str:
    f = float(number)
    n = str(int(f)) if f == int(f) else f'{f:g}'
    name = re.sub('\\s*\\(\\d{4}\\)\\s*$', '', series_title or '').strip()
    name = re.sub('\\s+-\\s+', ': ', name)
    return f'{name} Vol {int(volume or 1)} {n}'

def parse_gallery(data: dict, title: str) -> list[dict] | None:
    q = (data or {}).get('query') or {}
    for r in q.get('redirects') or []:
        if r.get('from') == title:
            title = r.get('to') or title
    pages = q.get('pages') or {}
    if any(('missing' in p and p.get('title') == title for p in pages.values())):
        return None
    prefix = f'File:{title} '
    out = []
    for p in pages.values():
        t = p.get('title') or ''
        info = (p.get('imageinfo') or [{}])[0]
        if not t.startswith(prefix) or not info.get('url'):
            continue
        if (info.get('mime') or 'image/jpeg') not in ('image/jpeg', 'image/png', 'image/webp'):
            continue
        if (info.get('width') or 0) < GALLERY_MIN_WIDTH:
            continue
        name = re.sub('\\.(jpe?g|png|webp)$', '', t[len(prefix):], flags=re.I).strip() or 'Cover'
        out.append({'name': name, 'url': info['url'], 'width': info.get('width'), 'height': info.get('height')})
    return sorted(out, key=lambda c: (c['name'].lower(), -(c['width'] or 0)))

def gallery(wiki: str, title: str) -> list[dict] | None:
    d = _get(WIKIS[wiki], action='query', generator='images', titles=title, gimlimit='max', prop='imageinfo', iiprop='url|size|mime', redirects='1')
    if (d.get('query') or {}).get('pages'):
        return parse_gallery(d, title)
    probe = _get(WIKIS[wiki], action='query', titles=title, redirects='1')
    pages = (probe.get('query') or {}).get('pages') or {}
    return None if any(('missing' in p for p in pages.values())) else []
