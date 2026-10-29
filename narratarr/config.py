"""Settings, persisted as JSON in the data directory."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get("NARRATARR_DATA", Path.home() / ".narratarr"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# The app used to be called Audiobookeer; carry its data over on first run.
_OLD_DATA_DIR = Path.home() / ".audiobookeer"
if "NARRATARR_DATA" not in os.environ and not DATA_DIR.exists() and _OLD_DATA_DIR.is_dir():
    _OLD_DATA_DIR.rename(DATA_DIR)


@dataclass
class Settings:
    rd_api_key: str = ""
    library_dir: str = os.environ.get("NARRATARR_LIBRARY") or str(Path.home() / "Audiobooks")
    folder_template: str = "{author}/{title}"
    abb_base_url: str = "https://audiobookbay.lu"
    max_concurrent: int = 2
    delete_from_rd_after: bool = True
    smart_search: bool = True
    search_provider: str = "audiobookbay"  # or "brave"
    brave_api_key: str = ""
    audiobookshelf_url: str = ""
    audiobookshelf_token: str = ""
    audiobookshelf_library_id: str = ""
    prowlarr_url: str = ""
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    sabnzbd_url: str = ""
    sabnzbd_api_key: str = ""
    sabnzbd_category: str = ""
    usenet_client: str = "sabnzbd"  # or "nzbget"
    nzbget_url: str = ""
    nzbget_username: str = ""
    nzbget_password: str = ""
    nzbget_category: str = ""
    torrent_client: str = "realdebrid"  # or "qbittorrent" / "transmission"
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    qbittorrent_category: str = "audiobooks"
    transmission_url: str = ""
    transmission_username: str = ""
    transmission_password: str = ""
    # When a download client sees its downloads under a different path (e.g. another container).
    path_remote: str = ""
    path_local: str = ""
    # Books dropped in here are moved into the library automatically.
    import_folder: str = ""
    # Look for Wanted books on Prowlarr (and LibriVox) every few hours.
    auto_search: bool = True
    auto_search_hours: int = 12
    audible_region: str = "us"

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        d = asdict(self)
        for key in SECRETS:
            d[key] = _mask(d[key])
        d["rd_configured"] = bool(self.rd_api_key)
        d["brave_configured"] = bool(self.brave_api_key)
        d["prowlarr_configured"] = bool(self.prowlarr_url and self.prowlarr_api_key)
        d["sabnzbd_configured"] = bool(self.sabnzbd_url and self.sabnzbd_api_key)
        d["nzbget_configured"] = bool(self.nzbget_url)
        d["qbittorrent_configured"] = bool(self.qbittorrent_url)
        d["transmission_configured"] = bool(self.transmission_url)
        d["usenet_configured"] = d[f"{self.usenet_client}_configured"]
        d["torrent_configured"] = d["rd_configured" if self.torrent_client == "realdebrid" else f"{self.torrent_client}_configured"]
        return d


SECRETS = (
    "rd_api_key", "audiobookshelf_token", "brave_api_key", "prowlarr_api_key", "sabnzbd_api_key",
    "nzbget_password", "qbittorrent_password", "transmission_password",
)
CHOICES = {
    "search_provider": ("audiobookbay", "brave"),
    "usenet_client": ("sabnzbd", "nzbget"),
    "torrent_client": ("realdebrid", "qbittorrent", "transmission"),
    "audible_region": ("us", "uk", "ca", "au", "in", "de", "fr", "it", "es", "jp"),
}
# Settings that were renamed; old settings files are read with the new names.
RENAMED = {"sabnzbd_remote_path": "path_remote", "sabnzbd_local_path": "path_local"}


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return "•" * 8 + secret[-4:]


_lock = threading.Lock()
_settings: Settings | None = None


def load() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            _settings = Settings()
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text())
                for old, new in RENAMED.items():
                    if old in raw and new not in raw:
                        raw[new] = raw[old]
                known = {f.name for f in fields(Settings)}
                for k, v in raw.items():
                    if k in known:
                        setattr(_settings, k, v)
        return _settings


def update(changes: dict) -> Settings:
    """Apply changes and persist. Masked secrets sent back unchanged are ignored."""
    s = load()
    with _lock:
        known = {f.name: f for f in fields(Settings)}
        for k, v in changes.items():
            if k not in known:
                continue
            if k in SECRETS and isinstance(v, str) and v.startswith("•"):
                continue
            if k == "max_concurrent":
                v = max(1, min(int(v), 8))
            elif k == "auto_search_hours":
                v = max(1, min(int(v), 168))
            elif k in CHOICES:
                v = v if v in CHOICES[k] else known[k].default
            elif k.endswith("_url") and isinstance(v, str):
                v = str(v).strip().rstrip("/")
            elif isinstance(v, str):
                v = v.strip()
            setattr(s, k, v)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(s), indent=2))
        os.chmod(SETTINGS_FILE, 0o600)
    return s
