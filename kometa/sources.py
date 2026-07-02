"""Source adapters — one place to get a configured client for every external
system Kometa talks to (Komga, SABnzbd, qBittorrent, Prowlarr, ComicVine,
LOCG, and the Usenet indexer list).

Each accessor reads current config straight from the DB and rebuilds its client
when credentials change, so callers never touch credentials or worry about
caching. This is the seam between Kometa's logic and the outside world.
"""
import os
import logging

from kometa.komga_client import KomgaClient
from kometa.sabnzbd_client import SABnzbdClient
from kometa.qbittorrent_client import QBittorrentClient
import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH


def comics_root() -> str:
    """The comics library root — the one path that actually has to be set.
    DB config (Settings) wins, then the COMICS_ROOT env (first-boot provisioning),
    then a sane container default. Read live so a Settings change takes effect."""
    return db.get_config(DB_PATH).get("comics_root") or os.environ.get("COMICS_ROOT") or "/comics"


def staging_dir() -> str:
    """Where downloads land for validation before being filed into the library.
    Derived as a hidden child of the comics root so it shares a filesystem (moves
    are atomic, not cross-mount copies) and stays out of the scanned library.
    KOMETA_DOWNLOADS overrides for anyone who wants a separate location."""
    return os.environ.get("KOMETA_DOWNLOADS") or os.path.join(comics_root(), ".kometa-staging")

# Cached clients — one live instance per config signature, rebuilt only when
# the relevant keys change. Building per-call was constructing a fresh
# requests.Session (and for qBit, doing a fresh LOGIN) on every scheduler tick
# and every queue item. A None build (unconfigured, or LOCG login failure) is
# never cached, so a transient failure can't poison the cache until restart.
_client_cache: dict = {}


def _cached(name: str, key: str, build):
    hit = _client_cache.get(name)
    if hit and hit[0] == key:
        return hit[1]
    client = build()
    if client is not None:
        _client_cache[name] = (key, client)
    return client


def komga() -> KomgaClient | None:
    cfg = db.get_config(DB_PATH)
    if not cfg.get("komga_url"):
        return None
    key = f"{cfg.get('komga_url')}|{cfg.get('komga_user')}|{cfg.get('komga_pass')}|{cfg.get('komga_library_id')}"
    return _cached("komga", key, lambda: KomgaClient(
        base_url=cfg.get("komga_url", ""),
        auth=(cfg.get("komga_user", ""), cfg.get("komga_pass", "")),
        library_id=cfg.get("komga_library_id", ""),
    ))


def sabnzbd() -> SABnzbdClient | None:
    cfg = db.get_config(DB_PATH)
    url = cfg.get("sab_url", "")
    key = cfg.get("sab_apikey", "")
    if not url or not key:
        return None
    return _cached("sabnzbd", f"{url}|{key}", lambda: SABnzbdClient(url, key))


def qbittorrent() -> QBittorrentClient | None:
    """The torrent download client — twin of sabnzbd(). Reach it by LAN IP from
    the Kometa container (qBit rejects the Tailscale host and needs auth)."""
    cfg = db.get_config(DB_PATH)
    url = cfg.get("qbit_url", "")
    user = cfg.get("qbit_user", "")
    pw = cfg.get("qbit_pass", "")
    if not url or not user:
        return None
    return _cached("qbittorrent", f"{url}|{user}|{pw}",
                   lambda: QBittorrentClient(url, user, pw))


def prowlarr():
    """Prowlarr aggregate-search client (sees usenet AND torrent indexers in one
    query). Returns None until prowlarr_url + prowlarr_apikey are configured."""
    cfg = db.get_config(DB_PATH)
    url = cfg.get("prowlarr_url", "")
    key = cfg.get("prowlarr_apikey", "")
    if not url or not key:
        return None
    from kometa.prowlarr_client import ProwlarrClient
    return _cached("prowlarr", f"{url}|{key}", lambda: ProwlarrClient(url, key))


def comicvine():
    """ComicVine search client — the gap-filler for LOCG. Returns None until
    cv_api_key is configured."""
    cfg = db.get_config(DB_PATH)
    key = cfg.get("cv_api_key", "")
    if not key:
        return None
    from kometa.comicvine_client import ComicVineClient
    return _cached("comicvine", key, lambda: ComicVineClient(key))


_wikipedia_instance = None


def wikipedia():
    """Wikipedia arc-discovery client — keyless, so always available."""
    global _wikipedia_instance
    if _wikipedia_instance is None:
        from kometa.wikipedia_client import WikipediaClient
        _wikipedia_instance = WikipediaClient()
    return _wikipedia_instance


def usenet_indexers() -> list[dict]:
    import json
    cfg = db.get_config(DB_PATH)
    raw = cfg.get("newznab_indexers", "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def locg():
    cfg = db.get_config(DB_PATH)
    user = cfg.get("locg_user", "")
    pw   = cfg.get("locg_pass", "")
    if not user or not pw:
        return None

    def _build():
        try:
            from kometa.locg_client import LOCGClient
            return LOCGClient(user, pw, session=cfg.get("locg_session") or None)
        except Exception as e:
            logger.warning(f"LoCG init failed: {e}")
            return None

    # Keyed on creds only — the client re-logs-in internally when its session
    # expires, so a cookie change must NOT invalidate the cache (that would
    # rebuild + re-login in a loop). Instead, persist any refreshed cookie on
    # every access so a restart resumes the live session instead of re-logging.
    client = _cached("locg", f"{user}|{pw}", _build)
    if client is not None and client.session_cookie and client.session_cookie != cfg.get("locg_session"):
        db.set_config({"locg_session": client.session_cookie}, DB_PATH)
    return client
