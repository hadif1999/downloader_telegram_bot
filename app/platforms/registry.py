from __future__ import annotations

import re

from app.platforms.base import Platform
from app.platforms.stubs import STUBS
from app.platforms.youtube import YouTube

PLATFORMS: list[Platform] = [YouTube(), *STUBS]
_BY_NAME = {p.name: p for p in PLATFORMS}

URL_RE = re.compile(r"https?://\S+|(?:www\.)?[\w-]+\.[a-z]{2,}/\S+", re.I)


def get(name: str) -> Platform:
    return _BY_NAME[name]


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def detect(text: str) -> tuple[Platform, str, str] | None:
    """Find the first supported link in text -> (platform, media_id, url)."""
    for url in find_urls(text):
        for p in PLATFORMS:
            media_id = p.match(url)
            if media_id:
                return p, media_id, url
    return None
