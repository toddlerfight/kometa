"""Source adapters — one place to get a configured client for every external
system Kometa talks to (Komga, Metron, ComicVine, SABnzbd, LOCG, and the Usenet
indexer list).

Each accessor reads current config straight from the DB and rebuilds its client
when credentials change, so callers never touch credentials or worry about
caching. This is the seam between Kometa's logic and the outside world.
"""
import os
import logging

from kometa.komga_client import KomgaClient
from kometa.metron_client import MetronClient
from kometa.comicvine_client import ComicVineClient
from kometa.sabnzbd_client import SABnzbdClient
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

# Cached clients — rebuilt only when the relevant config key changes.
_komga_instance: "KomgaClient | None" = None
_komga_cfg_key: str = ""
_metron_instance: "MetronClient | None" = None
_metron_cfg_key: str = ""


def komga() -> KomgaClient | None:
    global _komga_instance, _komga_cfg_key
    cfg = db.get_config(DB_PATH)
    if not cfg.get("komga_url"):
        return None
    key = f"{cfg.get('komga_url')}|{cfg.get('komga_user')}|{cfg.get('komga_pass')}|{cfg.get('komga_library_id')}"
    if _komga_instance is None or key != _komga_cfg_key:
        _komga_instance = KomgaClient(
            base_url=cfg.get("komga_url", ""),
            auth=(cfg.get("komga_user", ""), cfg.get("komga_pass", "")),
            library_id=cfg.get("komga_library_id", ""),
        )
        _komga_cfg_key = key
    return _komga_instance


def metron() -> MetronClient:
    global _metron_instance, _metron_cfg_key
    cfg = db.get_config(DB_PATH)
    key = f"{cfg.get('metron_user')}|{cfg.get('metron_pass')}"
    if _metron_instance is None or key != _metron_cfg_key:
        _metron_instance = MetronClient(auth=(cfg.get("metron_user", ""), cfg.get("metron_pass", "")))
        _metron_cfg_key = key
    return _metron_instance


def comicvine() -> ComicVineClient | None:
    key = db.get_config(DB_PATH).get("cv_api_key", "")
    return ComicVineClient(key) if key else None


def sabnzbd() -> SABnzbdClient | None:
    cfg = db.get_config(DB_PATH)
    url = cfg.get("sab_url", "")
    key = cfg.get("sab_apikey", "")
    return SABnzbdClient(url, key) if url and key else None


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
    try:
        from kometa.locg_client import LOCGClient
        client = LOCGClient(user, pw, session=cfg.get("locg_session") or None)
        # Persist refreshed session if it changed (re-login happened)
        if client.session_cookie and client.session_cookie != cfg.get("locg_session"):
            db.set_config({"locg_session": client.session_cookie}, DB_PATH)
        return client
    except Exception as e:
        logger.warning(f"LoCG init failed: {e}")
        return None
