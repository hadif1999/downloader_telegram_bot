from __future__ import annotations

import re

from app.config import get_settings
from app.platforms.base import Platform
from app.platforms.sites import VK, Facebook, Instagram, Likee, Pinterest, Snapchat, SoundCloud, TikTok
from app.platforms.spotify import Spotify
from app.platforms.threads import Threads
from app.platforms.youtube import YouTube

PLATFORMS: list[Platform] = [
    YouTube(),
    Instagram(),
    TikTok(),
    Pinterest(),
    Snapchat(),
    Likee(),
    VK(),
    Facebook(),
    Threads(),
    SoundCloud(),
    Spotify(),
]
PLATFORMS = [p for p in PLATFORMS if getattr(get_settings(), f"{p.title.lower()}_enabled")]
_BY_NAME = {p.name: p for p in PLATFORMS}

URL_RE = re.compile(r"https?://\S+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/\S+", re.I)


def get(name: str) -> Platform:
    return _BY_NAME[name]


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def match(url: str) -> tuple[Platform, str] | None:
    for p in PLATFORMS:
        if media_id := p.match(url):
            return p, media_id
    return None


def detect_all(text: str, limit: int | None = None) -> tuple[list[tuple[Platform, str, str]], int]:
    """Every supported link in text, in order and without repeats -> ([(platform, media_id, url)],
    number of links left out: unsupported ones and those beyond `limit`)."""
    found: list[tuple[Platform, str, str]] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for url in find_urls(text):
        url = url.rstrip(".,;:!?)]}>»\"'")  # punctuation that ends a sentence, not the link
        if not url.lower().startswith("http"):
            url = "https://" + url
        hit = match(url)
        if hit is None:
            skipped += 1
            continue
        platform, media_id = hit
        if (platform.name, media_id) in seen:
            continue
        if limit is not None and len(found) >= limit:
            skipped += 1
            continue
        seen.add((platform.name, media_id))
        found.append((platform, media_id, url))
    return found, skipped


def detect(text: str) -> tuple[Platform, str, str] | None:
    """Find the first supported link in text -> (platform, media_id, url)."""
    found, _ = detect_all(text, 1)
    return found[0] if found else None
