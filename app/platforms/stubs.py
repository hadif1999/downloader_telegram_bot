"""Platforms that are planned but not implemented yet (see docs/TODO.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class StubPlatform:
    name: str
    title: str
    emoji: str
    pattern: str
    implemented: bool = False

    def match(self, url: str) -> str | None:
        m = re.search(self.pattern, url, re.I)
        return m.group(0) if m else None

    def url_for(self, media_id: str) -> str:
        return media_id

    def extract(self, url: str):
        raise NotImplementedError(f"{self.title} is not supported yet")

    def download(self, *args, **kwargs):
        raise NotImplementedError(f"{self.title} is not supported yet")


STUBS = [
    StubPlatform("ig", "Instagram", "📸", r"(?:www\.)?instagram\.com/\S+"),
    StubPlatform("tt", "TikTok", "🎵", r"(?:\w+\.)?tiktok\.com/\S+"),
    StubPlatform("pin", "Pinterest", "📌", r"(?:\w+\.)?(?:pinterest\.\w+|pin\.it)/\S+"),
    StubPlatform("sc", "Snapchat", "👻", r"(?:www\.)?snapchat\.com/\S+"),
    StubPlatform("lk", "Likee", "💜", r"(?:\w+\.)?likee\.(?:video|com)/\S+"),
    StubPlatform("vk", "VK", "🅥", r"(?:\w+\.)?(?:vk\.com|vkvideo\.ru)/\S+"),
    StubPlatform("fb", "Facebook", "📘", r"(?:\w+\.)?(?:facebook\.com|fb\.watch)/\S+"),
    StubPlatform("th", "Threads", "🧵", r"(?:www\.)?threads\.(?:net|com)/\S+"),
    StubPlatform("mu", "Music", "🎶", r"(?:open\.spotify\.com|soundcloud\.com|music\.apple\.com)/\S+"),
]
