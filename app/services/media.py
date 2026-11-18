"""Resolving MediaInfo: Redis cache first, then (re-)extraction from the original link."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram.types import FSInputFile

from app.config import get_settings
from app.models import MediaInfo
from app.platforms import registry
from app.platforms.common import download_file, guess_ext
from app.services import cache

# Concurrent metadata lookups in the bot (each may run deno for YouTube, ~125 MB with its heap cap).
extraction_slots = asyncio.Semaphore(get_settings().extract_slots)


async def resolve_async(platform_name: str, media_id: str, url: str | None = None) -> MediaInfo | None:
    async with extraction_slots:
        return await asyncio.to_thread(resolve, platform_name, media_id, url)


@asynccontextmanager
async def thumbnail_file(url: str):
    with tempfile.TemporaryDirectory(prefix="media-thumb-") as directory:
        path = Path(directory) / f"thumbnail.{guess_ext(url, 'jpg')}"
        await asyncio.to_thread(download_file, url, path, timeout=15, max_bytes=5 * 1024 * 1024)
        yield FSInputFile(path)


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
    if info.audio_tracks or info.kind == "collection":
        return None
    if info.kind == "gallery":
        return "all"
    if info.kind == "audio":
        return "mp3"
    video = [f for f in info.formats if f.key != "mp3"]
    limit = get_settings().max_file_bytes
    if video and all(f.size is not None and f.size > limit for f in video):
        return None  # keep the MP3/description controls available
    return video[0].key if len(video) == 1 else None


def preset_choice(info: MediaInfo, preset: str) -> str | None:
    """Format for one batch item under the batch's quality preset (batches.PRESETS: a video height
    cap or "mp3"); None when nothing fits the
    upload limit. Audio-only media is always MP3 and photo posts are sent whole."""
    if info.kind == "gallery":
        return "all"
    if info.kind == "collection":
        return None
    if info.kind == "audio" or preset == "mp3":
        return "mp3" if info.format("mp3") else None
    limit = get_settings().max_file_bytes
    video = [f for f in info.formats if f.key != "mp3" and (f.size is None or f.size <= limit)]
    if not video:
        return None
    cap = int(preset) if preset.isdigit() else 1080
    below = [f for f in video if (f.height or 0) <= cap]
    if below:
        return max(below, key=lambda f: f.height or 0).key
    return min(video, key=lambda f: f.height or 0).key


def valid_choice(info: MediaInfo, fmt: str, lang: str | None) -> bool:
    if info.kind == "collection":
        return False
    if info.kind == "gallery":
        return fmt == "all" and not lang
    return bool(info.format(fmt)) and (not lang or any(t.lang == lang for t in info.audio_tracks))
