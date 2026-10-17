"""Source adapters — one place to get a configured client for every external
system Kometa talks to (SABnzbd, qBittorrent, Prowlarr, ComicVine, Wikipedia).
Komga was one until it was retired (2026-10-12); nothing talks to it now.

Each accessor reads current config straight from the DB and rebuilds its client
when credentials change, so callers never touch credentials or worry about
caching. This is the seam between Kometa's logic and the outside world.
"""
import os
import logging

from kometa.sabnzbd_client import SABnzbdClient, SAB_PRIORITY
from kometa.qbittorrent_client import QBittorrentClient
import kometa.db as db

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH


def comics_root() -> str:
    """The comics library root — the one path that actually has to be set.
    DB config (Settings) wins, then the COMICS_ROOT env (first-boot provisioning),
    then a sane container default. Read live so a Settings change takes effect."""
    return db.get_config(DB_PATH).get("comics_root") or os.environ.get("COMICS_ROOT") or "/comics"


class OutsideLibrary(ValueError):
    """A path Kometa won't act on: the 2026-10-11 security review found any folder path,
    and the comics root itself, could be pointed anywhere in the container — and Remove,
    tidy and placement then act on whatever is there."""


def check_series_folder(path: str, root: str | None = None) -> str:
    """A series folder sits at least Publisher/Series deep inside the comics root, and not
    in the trash or a hidden folder: the root or a publisher folder would hand Remove and
    tidy everything under it. Returns the path as given; raises OutsideLibrary."""
    r = os.path.realpath(root or comics_root())
    rel = os.path.relpath(os.path.realpath(path), r)
    parts = rel.split(os.sep)
    if rel == "." or parts[0] == ".." or os.path.isabs(rel):
        raise OutsideLibrary(f"{path} isn't inside the comics folder")
    if len(parts) < 2:
        raise OutsideLibrary(f"{path} is a publisher folder, not a series folder")
    if parts[0] == "_trash" or any(p.startswith(".") for p in parts):
        raise OutsideLibrary(f"{path} is in the trash or a hidden folder")
    return path


_NEVER_ROOTS = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/root")
_NEVER_CONTAIN = ("/var", "/home", "/Users", "/Volumes", "/mnt", "/media")   # a library may live under these, not be one


def check_comics_root(path: str) -> str:
    """The comics root: an existing folder that isn't '/', the app's data or code, or a
    system folder — and doesn't contain them either. Returns the path; raises OutsideLibrary."""
    p = os.path.realpath(path)
    if p == "/" or not os.path.isdir(p):
        raise OutsideLibrary(f"{path} isn't a folder Kometa can use")
    code = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    data = os.path.dirname(os.path.realpath(db.DB_PATH))
    for g in (code, data, *_NEVER_ROOTS):
        g = os.path.realpath(g)
        if p == g or p.startswith(g + os.sep) or g.startswith(p + os.sep):
            raise OutsideLibrary(f"{path} overlaps {g}")
    for g in _NEVER_CONTAIN:
        if p == os.path.realpath(g):
            raise OutsideLibrary(f"{path} is a whole system folder")
    return path


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


def sabnzbd() -> SABnzbdClient | None:
    cfg = db.get_config(DB_PATH)
    url = cfg.get("sab_url", "")
    key = cfg.get("sab_apikey", "")
    if not url or not key:
        return None
    try:
        prio = int(cfg.get("sab_priority") or SAB_PRIORITY)  # -1 low · 0 normal · 1 high (2 force is clamped)
    except ValueError:
        prio = SAB_PRIORITY
    jump = (cfg.get("sab_jump_queue") or "1") not in ("0", "false", "off")
    return _cached("sabnzbd", f"{url}|{key}|{prio}|{jump}", lambda: SABnzbdClient(url, key, prio, jump))


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
    cv_api_key is configured, or when the metadata toggle is off."""
    cfg = db.get_config(DB_PATH)
    if cfg.get("comicvine_enabled", "1") == "0":
        return None
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


# NB: no locg() accessor — LOCG is keyless. The login was fully redundant (the
# search endpoint ignores auth), so callers use kometa.locg_client's anonymous
# functions directly (search_series_anon, get_issues_anon, fetch_variants, …).
