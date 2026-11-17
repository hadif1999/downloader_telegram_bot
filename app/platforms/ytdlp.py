"""Generic yt-dlp based platform, with gallery-dl and OpenGraph fallbacks for photo posts."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yt_dlp.plugins import load_all_plugins
from yt_dlp.utils import DownloadError

from app.config import get_settings
from app.models import DownloadResult, FormatOption, GalleryItem, MediaInfo
from app.platforms import gallery
from app.platforms.base import ProgressCb
from app.platforms.common import (
    BROWSER_HEADERS,
    CRAWLER_UA,
    FLUSH_BYTES,
    TooLargeError,
    cookie_file,
    download_file,
    fetch,
    og_tags,
    release_page_cache,
    url_key,
)
from app.platforms.downloader import YoutubeDL
from app.platforms.network import on_domains

# Load yt-dlp plugins (bgutil PO-token provider) once, in the main thread: importing them lazily
# from several worker threads at the same time races and breaks the plugin registry.
load_all_plugins()

STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


TooLarge = TooLargeError  # raised when a file exceeds MAX_FILE_MB


class UserError(Exception):
    """Error whose message is safe and useful to show to the user."""


# ---------------------------------------------------------------- format helpers


def _size(f: dict, duration: float | None = None) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:  # HLS/DASH manifests often lack sizes
        size = int(f["tbr"] * 125 * duration)
    return size


AUDIO_EXT = {"mp3", "m4a", "opus", "ogg", "oga", "wav", "flac", "aac", "weba"}


def _has_video(f: dict) -> bool:
    if f.get("vcodec") == "none":
        return False
    return bool(f.get("height") or f.get("vcodec") or f.get("ext") not in AUDIO_EXT)


def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def _quality(f: dict) -> int | None:
    """Resolution class ("1080" in "1080p"), using the short side so vertical videos work."""
    w, h = f.get("width"), f.get("height")
    if not h:
        return None
    side = min(w, h) if w else h
    return min(STANDARD_HEIGHTS, key=lambda s: abs(s - side))


def is_original_audio(f: dict) -> bool:
    note = (f.get("format_note") or "").lower()
    return "original" in note or (f.get("language_preference") or 0) >= 10


def audio_formats(formats: list[dict]) -> list[dict]:
    return [f for f in formats if f.get("vcodec") == "none" and _has_audio(f)]


def _best_audio_size(formats: list[dict], duration: float | None) -> int | None:
    audio = audio_formats(formats)
    m4a = [f for f in audio if f.get("ext") == "m4a"] or audio
    original = [f for f in m4a if is_original_audio(f)] or m4a
    sizes = [s for f in original if (s := _size(f, duration))]
    return max(sizes) if sizes else None


def mp3_size(duration: float | None) -> int | None:
    # Output is encoded at 192 kbps; source codec/bitrate is unrelated to this size.
    return int(duration * 24_000) + 65_536 if duration else None


def build_formats(info: dict) -> tuple[str, list[FormatOption]]:
    """-> (kind, options). Video resolutions (largest first) plus MP3."""
    formats = info.get("formats") or [info]
    duration = info.get("duration")
    audio_size = _best_audio_size(formats, duration)
    video = [f for f in formats if _has_video(f)]
    if not video:
        return "audio", [FormatOption("mp3", "MP3", mp3_size(duration))]

    groups: dict[int, list[dict]] = {}
    for f in video:
        if (q := _quality(f)) is not None:
            groups.setdefault(q, []).append(f)
    out = []
    for q in sorted(groups, reverse=True):
        fs = groups[q]
        # the download prefers h264 (Telegram-friendly), so estimate with it when present
        avc = [f for f in fs if (f.get("vcodec") or "").startswith(("avc", "h264"))] or fs
        # real sizes beat bitrate estimates (HLS variants of the same stream have no size)
        known = [f for f in avc if f.get("filesize") or f.get("filesize_approx")] or avc
        sizes = [s + (0 if _has_audio(f) else (audio_size or 0)) for f in known if (s := _size(f, duration))]
        out.append(FormatOption(str(q), f"{q}p", max(sizes) if sizes else None, max(f["height"] for f in fs)))
    if not out:  # no resolution info at all
        out.append(FormatOption("best", "Video", _size(video[-1], duration)))
    out.append(FormatOption("mp3", "MP3", mp3_size(duration)))
    return "video", out


def format_selector(height: int | None, lang: str | None = None, audio: bool = False) -> str:
    lang_f = f"[language={lang}]" if lang else ""
    if audio:
        return f"ba[ext=m4a]{lang_f}/ba{lang_f}/b{lang_f}"
    h = f"[height<={height}]" if height else ""
    return f"bv*{h}[ext=mp4]+ba[ext=m4a]{lang_f}/bv*{h}+ba{lang_f}/b{h}{lang_f}"


def to_date(info: dict) -> str | None:
    d = info.get("upload_date")
    if d and len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    if ts := info.get("timestamp"):
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    return None


class Progress:
    """Merges yt-dlp per-file progress (video + audio streams) into one counter.

    Also flushes each file being written and drops it from the page cache every FLUSH_BYTES, so
    a multi-gigabyte download doesn't fill the container's memory with dirty pages."""

    def __init__(self, cb: ProgressCb, expected: int | None, flush_bytes: int = FLUSH_BYTES):
        self.cb, self.expected, self.done, self._seen = cb, expected, 0, set()
        self.flush_bytes = flush_bytes
        self._released: dict[str, int] = {}

    def __call__(self, d: dict) -> None:
        name = d.get("filename") or d.get("tmpfilename")
        if d["status"] == "downloading":
            cur = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            grand = self.expected or (self.done + total if total else None)
            self.cb(self.done + cur, max(grand, self.done + cur) if grand else None)
            writing = d.get("tmpfilename") or name
            if writing and cur - self._released.get(writing, 0) >= self.flush_bytes:
                release_page_cache(writing)
                self._released[writing] = cur
        elif d["status"] == "finished" and name not in self._seen:
            self._seen.add(name)
            self.done += d.get("downloaded_bytes") or d.get("total_bytes") or 0
            if name:
                release_page_cache(name)


# ---------------------------------------------------------------- platform


class YtDlpPlatform:
    name = "generic"
    title = "Generic"
    emoji = "🌐"
    patterns: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    gallery = False  # photo posts / carousels through gallery-dl
    opengraph = False  # last resort: og:video / og:image of the page
    use_ytdlp = True  # False when yt-dlp's extractor for the site is known to be broken

    def __init__(self) -> None:
        self._res = [re.compile(p, re.I) for p in self.patterns]

    # --- identity
    def match(self, url: str) -> str | None:
        return (
            url_key(url) if on_domains(url, self.domains) and any(r.search(url) for r in self._res) else None
        )

    def url_for(self, media_id: str) -> str | None:
        return None  # resolved from the cached source url

    # --- yt-dlp options
    def ydl_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 30,
        }
        if cookies := cookie_file(self.title.lower()):
            opts["cookiefile"] = str(cookies)
        return opts

    # --- extraction
    def extract(self, url: str) -> MediaInfo:
        media_id = self.match(url)
        if not media_id:
            raise UserError("This URL does not belong to the selected platform.")
        error: Exception | None = None
        if not self.use_ytdlp:
            return self._fallbacks(url, media_id, None)
        try:
            with YoutubeDL({**self.ydl_opts(), "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            if info.get("_type") == "playlist":  # carousel / multi-video post
                if playlist := self.playlist_info(info, url, media_id):
                    return playlist
                entries = [e for e in info.get("entries") or [] if e]
                if self.gallery and len(entries) > 1 and (g := self._gallery(url, media_id)):
                    return g
                if len(entries) > 1:
                    raise UserError("Couldn't read every item in this post. Please try again later.")
                info = {**info, **entries[0]} if entries else info
            if info.get("formats") or info.get("url"):
                return self.to_media_info(info, url, media_id)
        except DownloadError as e:
            error = e
        return self._fallbacks(url, media_id, error)

    def playlist_info(self, info: dict, url: str, media_id: str) -> MediaInfo | None:
        return None

    def _fallbacks(self, url: str, media_id: str, error: Exception | None) -> MediaInfo:
        if self.opengraph and not self.use_ytdlp and (o := self._opengraph(url, media_id)):
            return o  # cheap single request first when yt-dlp is skipped
        if self.gallery and (g := self._gallery(url, media_id)):
            return g
        if self.opengraph and (o := self._opengraph(url, media_id)):
            return o
        raise error or UserError("No downloadable media found in this post.")

    def to_media_info(self, info: dict, url: str, media_id: str) -> MediaInfo:
        kind, formats = build_formats(info)
        title = info.get("title") or info.get("description") or self.title
        return MediaInfo(
            platform=self.name,
            id=media_id,
            url=url,
            title=title.strip()[:200],
            uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
            uploader_url=info.get("uploader_url") or info.get("channel_url"),
            duration=int(info["duration"]) if info.get("duration") else None,
            views=info.get("view_count"),
            likes=info.get("like_count"),
            comments=info.get("comment_count"),
            upload_date=to_date(info),
            thumbnail=info.get("thumbnail"),
            description=info.get("description") or "",
            formats=formats,
            kind=kind,
        )

    def _gallery(self, url: str, media_id: str) -> MediaInfo | None:
        try:
            meta, items = gallery.extract(url, cookie_file(self.title.lower()))
        except Exception:
            return None
        if not items:
            return None
        return gallery.to_media_info(self.name, media_id, url, self.title, meta, items)

    def _opengraph(self, url: str, media_id: str) -> MediaInfo | None:
        tags: dict[str, str] = {}
        for headers in (BROWSER_HEADERS, {"User-Agent": CRAWLER_UA}):
            try:
                tags = og_tags(fetch(url, headers)[1])
            except Exception:
                continue
            if tags.get("og:video") or tags.get("og:video:secure_url") or tags.get("og:image"):
                break
        video = tags.get("og:video:secure_url") or tags.get("og:video")
        image = tags.get("og:image")
        if not (video or image):
            return None
        info = MediaInfo(
            platform=self.name,
            id=media_id,
            url=url,
            title=(tags.get("og:title") or self.title)[:200],
            description=tags.get("og:description") or "",
            thumbnail=image,
        )
        if video:
            info.extra["direct_url"] = video
            info.formats = [FormatOption("best", "Video", None), FormatOption("mp3", "MP3", None)]
        else:
            info.kind = "gallery"
            info.items = [GalleryItem(image or "", "photo")]
        return info

    # --- download
    def download(
        self, info: MediaInfo, fmt: str, lang: str | None, dest: Path, progress: ProgressCb
    ) -> DownloadResult:
        if info.kind == "gallery":
            return gallery.download(info.items, dest, progress, self.ydl_opts(), referer=info.url)
        if direct := info.extra.get("direct_url"):
            return self.download_direct(direct, info, fmt, dest, progress)
        return self.download_url(info.url, info, fmt, lang, dest, progress)

    def download_direct(
        self, url: str, info: MediaInfo, fmt: str, dest: Path, progress: ProgressCb
    ) -> DownloadResult:
        """Direct CDN links (OpenGraph, Threads): plain HTTP; yt-dlp rejects odd extensions."""
        dest.mkdir(parents=True, exist_ok=True)
        video = download_file(
            url,
            dest / "media.mp4",
            referer=info.url,
            progress=progress,
            max_bytes=get_settings().max_source_bytes if fmt == "mp3" else get_settings().max_file_bytes,
        )
        result = DownloadResult(
            path=str(video), duration=info.duration, title=info.title, performer=info.uploader
        )
        if fmt == "mp3":
            mp3 = dest / "media.mp3"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-b:a", "192k", str(mp3)]
            subprocess.run(cmd, check=True, timeout=600)
            release_page_cache(mp3)
            result.path = str(mp3)
        return result

    def download_url(
        self, source: str, info: MediaInfo, fmt: str, lang: str | None, dest: Path, progress: ProgressCb
    ) -> DownloadResult:
        fo = info.format(fmt)
        if fo is None:
            raise UserError("This format is no longer available. Please send the link again.")
        audio = fmt == "mp3"
        opts: dict[str, Any] = {
            **self.ydl_opts(),
            "outtmpl": str(dest / "media.%(ext)s"),
            "format": format_selector(fo.height if fo and not audio else None, lang, audio),
            "progress_hooks": [Progress(progress, fo.size if fo else None)],
            "max_filesize": get_settings().max_source_bytes if audio else get_settings().max_file_bytes,
            # Fetch HTTP formats as byte-range parts (also avoids YouTube's per-request throttling);
            # each part streams to disk, so memory is independent of the file size.
            "http_chunk_size": get_settings().download_part_bytes,
        }
        if audio:
            opts |= {
                "writethumbnail": True,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
            }
        else:
            opts |= {"merge_output_format": "mp4", "format_sort": ["res", "vcodec:h264", "acodec:aac"]}
            if lang:
                # Keep the chosen audio even when bv* is a progressive video. The merger must
                # discard embedded audio from input 0 rather than silently dropping input 1.
                opts |= {
                    "allow_multiple_audio_streams": True,
                    "postprocessor_args": {"merger+ffmpeg_o": ["-map", "-0:a?"]},
                }
        with YoutubeDL(opts) as ydl:
            r = ydl.extract_info(source, download=True) or {}
        if r.get("_type") == "playlist":
            r = next((e for e in r.get("entries") or [] if e), r)
        if not r.get("requested_downloads"):  # skipped by max_filesize
            raise TooLarge(info.id)
        release_page_cache(r["requested_downloads"][0]["filepath"])  # merged/converted output
        return DownloadResult(
            path=r["requested_downloads"][0]["filepath"],
            width=r.get("width"),
            height=r.get("height"),
            duration=int(r["duration"]) if r.get("duration") else info.duration,
            title=info.title if info.platform != "yt" else r.get("title"),
            performer=info.uploader or r.get("uploader"),
        )
