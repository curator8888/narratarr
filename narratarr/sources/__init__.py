from .. import config
from .archive import ArchiveSource, librivox, oldtimeradio
from .audiobookbay import AudiobookBay
from .base import Category, Details, Listing, Page, Source

ORDER = ("librivox", "oldtimeradio", "audiobookbay")


def all_sources() -> dict[str, Source]:
    return {
        "audiobookbay": AudiobookBay(config.load().abb_base_url),
        "librivox": librivox(),
        "oldtimeradio": oldtimeradio(),
    }


def get_source(name: str = "audiobookbay") -> Source:
    sources = all_sources()
    if name not in sources:
        raise ValueError(f"Unknown source: {name}")
    return sources[name]


def source_for_url(url: str) -> Source | None:
    """Which source a book link belongs to (archive.org links are told apart later by the book's source name)."""
    for source in all_sources().values():
        if source.owns(url):
            return source
    return None


__all__ = [
    "ArchiveSource", "AudiobookBay", "Category", "Details", "Listing", "Page", "Source",
    "all_sources", "get_source", "source_for_url",
]
