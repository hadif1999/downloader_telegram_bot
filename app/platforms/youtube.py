"""YouTube: multi-language audio tracks, PO-token provider, verified thumbnails."""

from __future__ import annotations

import re
import urllib.request
from typing import Any

from app.config import get_settings
from app.models import AudioTrack, MediaInfo
from app.platforms.common import cookie_file
from app.platforms.network import on_domains, open_url
from app.platforms.ytdlp import YtDlpPlatform, audio_formats, is_original_audio

ID_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([\w-]{11})",
    re.I,
)


def build_audio_tracks(info: dict) -> list[AudioTrack]:
    langs: dict[str, bool] = {}
    for f in audio_formats(info.get("formats") or []):
        lang = f.get("language")
        if lang:
            langs[lang] = langs.get(lang, False) or is_original_audio(f)
    if len(langs) < 2:
        return []
    return [
        AudioTrack(lang=lang, label=lang.upper(), original=orig)
        for lang, orig in sorted(langs.items(), key=lambda kv: (not kv[1], kv[0]))
    ]


def _url_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with open_url(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def best_thumbnail(info: dict, check=None) -> str | None:
    """Best JPEG thumbnail that actually exists (yt-dlp lists unverified ones; maxres often 404s,
    and Telegram can't always fetch webp by URL). hqdefault.jpg always exists."""
    check = check or _url_ok
    thumbs = [t for t in info.get("thumbnails") or [] if ".jpg" in (t.get("url") or "")]
    thumbs.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0), reverse=True)
    for t in thumbs[:3]:
        if check(t["url"]):
            return t["url"]
    return f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"


class YouTube(YtDlpPlatform):
    name = "yt"
    title = "YouTube"
    emoji = "▶️"

    def match(self, url: str) -> str | None:
        if not on_domains(url, ("youtube.com", "youtu.be")):
            return None
        m = ID_RE.search(url)
        return m.group(1) if m else None

    def url_for(self, media_id: str) -> str:
        return f"https://www.youtube.com/watch?v={media_id}"

    def ydl_opts(self) -> dict[str, Any]:
        opts = super().ydl_opts()
        if cookies := cookie_file("youtube"):
            opts["cookiefile"] = str(cookies)
        if url := get_settings().pot_provider_url:
            opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [url]}}
        return opts

    def to_media_info(self, info: dict, url: str, media_id: str) -> MediaInfo:
        mi = super().to_media_info(info, self.url_for(info["id"]), info["id"])
        handle = info.get("uploader_id")
        mi.url = info.get("webpage_url") or mi.url
        mi.uploader = info.get("channel") or info.get("uploader")
        mi.uploader_handle = handle if handle and handle.startswith("@") else None
        mi.subscribers = info.get("channel_follower_count")
        mi.category = (info.get("categories") or [None])[0]
        mi.thumbnail = best_thumbnail(info)
        mi.audio_tracks = build_audio_tracks(info)
        return mi


def to_media_info(info: dict) -> MediaInfo:
    """Convenience for tests / scripts."""
    return YouTube().to_media_info(info, "", info["id"])
