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
    # When SABnzbd sees its downloads under a different path (e.g. another container).
    sabnzbd_remote_path: str = ""
    sabnzbd_local_path: str = ""

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        d = asdict(self)
        for key in SECRETS:
            d[key] = _mask(d[key])
        d["rd_configured"] = bool(self.rd_api_key)
        d["brave_configured"] = bool(self.brave_api_key)
        d["prowlarr_configured"] = bool(self.prowlarr_url and self.prowlarr_api_key)
        d["sabnzbd_configured"] = bool(self.sabnzbd_url and self.sabnzbd_api_key)
        return d


SECRETS = ("rd_api_key", "audiobookshelf_token", "brave_api_key", "prowlarr_api_key", "sabnzbd_api_key")


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
            elif k == "search_provider":
                v = v if v in ("audiobookbay", "brave") else "audiobookbay"
            elif k in ("abb_base_url", "prowlarr_url", "sabnzbd_url"):
                v = str(v).strip().rstrip("/")
            elif isinstance(v, str):
                v = v.strip()
            setattr(s, k, v)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(s), indent=2))
        os.chmod(SETTINGS_FILE, 0o600)
    return s
