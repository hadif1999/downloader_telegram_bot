"""Resolving MediaInfo: Redis cache first, then (re-)extraction from the original link."""

from __future__ import annotations

from app.models import MediaInfo
from app.platforms import registry
from app.services import cache


def resolve(platform_name: str, media_id: str, url: str | None = None) -> MediaInfo | None:
    """Blocking (runs yt-dlp etc.): call through asyncio.to_thread from async code."""
    platform = registry.get(platform_name)
    if info := cache.load_info(platform_name, media_id):
        return info
    url = url or cache.load_src(platform_name, media_id) or platform.url_for(media_id)
    if not url:
        return None
    info = platform.extract(url)
    info.id = media_id  # keep the id used in buttons/cache keys
    cache.save_info(info)
    return info


def single_choice(info: MediaInfo) -> str | None:
    """Format to start right away when the menu would have only one sensible button."""
    if info.audio_tracks:
        return None
    if info.kind == "gallery":
        return "all"
    if info.kind == "audio":
        return "mp3"
    video = [f for f in info.formats if f.key != "mp3"]
    return video[0].key if len(video) == 1 else None
