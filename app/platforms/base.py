from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.models import DownloadResult, MediaInfo

# progress callback: (done, total_or_None) - bytes, or item count for galleries
ProgressCb = Callable[[int, int | None], None]


class Platform(Protocol):
    name: str  # short key used in callback data / cache keys, e.g. "yt"
    title: str  # human name, e.g. "YouTube"
    emoji: str

    def match(self, url: str) -> str | None:
        """Return a media id (<= 22 chars) if the url belongs to this platform."""

    def url_for(self, media_id: str) -> str | None: ...

    def extract(self, url: str) -> MediaInfo: ...

    def download(
        self, info: MediaInfo, fmt: str, lang: str | None, dest: Path, progress: ProgressCb
    ) -> DownloadResult: ...
