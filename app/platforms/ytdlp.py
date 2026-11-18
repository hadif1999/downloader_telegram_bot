"""Generic yt-dlp based platform, with gallery-dl and OpenGraph fallbacks for photo posts."""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yt_dlp.networking.exceptions import HTTPError as YtHTTPError
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.plugins import load_all_plugins
from yt_dlp.utils import DownloadCancelled, DownloadError

from app.config import get_settings
from app.models import CollectionEntry, DownloadResult, FormatOption, GalleryItem, MediaInfo
from app.platforms import gallery
from app.platforms.base import ProgressCb
from app.platforms.common import (
    BROWSER_HEADERS,
    CRAWLER_UA,
    FLUSH_BYTES,
    TooLargeError,
    Writeback,
    cookie_file,
    describe,
    download_file,
    fetch,
    network_error,
    og_tags,
    release_page_cache,
    retry_delay,
    url_key,
)
from app.platforms.downloader import YoutubeDL
from app.platforms.network import on_domains

log = logging.getLogger(__name__)

# Load yt-dlp plugins (bgutil PO-token provider) once, in the main thread: importing them lazily
# from several worker threads at the same time races and breaks the plugin registry.
load_all_plugins()

STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


TooLarge = TooLargeError  # raised when a file exceeds MAX_FILE_MB


class UserError(Exception):
    """Error whose message is safe and useful to show to the user."""


# --- cached stream data
# The bot's lookup already has every format URL. The worker reuses it instead of extracting the
# link again (~2 s per download). Kept as long as the format card (INFO_CACHE_TTL), but never
# beyond STREAMS_TTL_MAX: YouTube's media URLs expire after ~6 h.
STREAMS_TTL_MAX = 4 * 3600
# Unused by downloads and most of the size (YouTube: 774 KB -> ~20 KB compressed without them).
STREAMS_DROP = (
    "automatic_captions",
    "subtitles",
    "heatmap",
    "requested_formats",
    "requested_downloads",
    "requested_subtitles",
)


def _streams_key(platform: str, media_id: str) -> str:
    from app.services.cache import namespace

    return f"streams:{namespace()}:{platform}:{media_id}"


def save_streams(platform: str, media_id: str, info: dict) -> None:
    """Remember an extraction result for the download. Best effort: a miss only costs a lookup."""
    try:
        from app.services.cache import client

        slim = {k: v for k, v in YoutubeDL.sanitize_info(info).items() if k not in STREAMS_DROP}
        slim["formats"] = [f for f in slim.get("formats") or [] if f.get("protocol") != "mhtml"]
        blob = base64.b64encode(zlib.compress(json.dumps(slim).encode(), 6)).decode()
        ttl = min(get_settings().info_cache_ttl, STREAMS_TTL_MAX)
        client().set(_streams_key(platform, media_id), blob, ex=ttl)
    except Exception:
        log.warning("couldn't cache stream data: platform=%s media=%s", platform, media_id)


def load_streams(platform: str, media_id: str) -> dict | None:
    try:
        from app.services.cache import client

        blob = client().get(_streams_key(platform, media_id))
        return json.loads(zlib.decompress(base64.b64decode(blob))) if blob else None
    except Exception:
        log.warning("couldn't read cached stream data: platform=%s media=%s", platform, media_id)
        return None


def drop_streams(platform: str, media_id: str) -> None:
    try:
        from app.services.cache import client

        client().delete(_streams_key(platform, media_id))
    except Exception:
        pass


# yt-dlp byte-range size for HTTP formats (see the comment where it is used).
YTDLP_CHUNK_BYTES = 10 << 20
# LAME's fastest algorithm at the same 192 kbps: ~32% faster MP3 encoding on one core
# (measured: 26 min of audio in 29 s instead of 43 s).
MP3_SPEED = ["-compression_level", "9"]


def _retry_sleep(n: int) -> int:
    return retry_delay(n + 1)  # yt-dlp passes the 0-based retry index


RETRYABLE_MESSAGE = re.compile(
    r"timed out|timeout|connection|reset by peer|incomplete|giving up after|"
    r"http error (403|408|429|5\d\d)|unable to download video data",
    re.I,
)


def retryable(error: BaseException | None) -> bool:
    """Whether re-running a failed download may succeed: network trouble, server errors, or an
    expired media URL (YouTube's 403s; a re-run extracts fresh URLs). Never user-facing errors,
    size limits, cancellation, or anything else deterministic."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (UserError, TooLargeError, DownloadCancelled)):
            return False
        if network_error(error):
            return True
        if isinstance(error, (TransportError, YtHTTPError)):
            return (
                not isinstance(error, YtHTTPError)
                or error.status in (403, 408, 425, 429)
                or error.status >= 500
            )
        if isinstance(error, DownloadError):
            cause = error.exc_info[1] if error.exc_info else None
            if cause is None:
                return bool(RETRYABLE_MESSAGE.search(str(error)))
            error = cause
            continue
        error = error.__cause__ or error.__context__
    return False


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

    Reports which stream is downloading, over how many connections (segmented HTTP downloads) or
    which HLS/DASH segment, and the merge / MP3 conversion steps that follow (as
    `postprocessor_hooks`). Also flushes each file yt-dlp writes itself and drops it from the
    page cache every FLUSH_BYTES, so a multi-gigabyte download doesn't fill the container's
    memory with dirty pages (the segmented downloader manages its own writeback)."""

    # yt-dlp postprocessor keys (class name without "FFmpeg"/"PP") -> progress stage
    POSTPROCESSORS = {
        "Merger": "merge",
        "ExtractAudio": "convert",
        "Metadata": "finalize",
        "EmbedThumbnail": "finalize",
    }

    def __init__(self, cb: ProgressCb, expected: int | None, flush_bytes: int = FLUSH_BYTES):
        self.cb, self.expected, self.done, self._seen = cb, expected, 0, set()
        self.position = 0  # bytes reported so far, across streams
        self.flush_bytes = flush_bytes
        self._writeback: dict[str, Writeback] = {}
        self._flushed: dict[str, int] = {}

    def __call__(self, d: dict) -> None:
        name = d.get("filename") or d.get("tmpfilename")
        if d["status"] == "downloading":
            cur = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            grand = self.expected or (self.done + total if total else None)
            self.position = self.done + cur
            self.cb(
                self.done + cur, max(grand, self.done + cur) if grand else None, **self._detail(d, cur, total)
            )
            writing = d.get("tmpfilename") or name
            if d.get("segmented"):
                return  # written at many offsets at once; the segmented downloader flushes it
            if writing and cur - self._flushed.get(writing, 0) >= self.flush_bytes:
                self._writeback.setdefault(writing, Writeback()).step_path(writing, cur)
                self._flushed[writing] = cur
        elif d["status"] == "finished" and name not in self._seen:
            self._seen.add(name)
            self.done += d.get("downloaded_bytes") or d.get("total_bytes") or 0
            if name:
                release_page_cache(name)

    def _detail(self, d: dict, cur: int, total: int | None) -> dict:
        detail: dict = {}
        info = d.get("info_dict") or {}
        video, audio = info.get("vcodec"), info.get("acodec")
        if video == "none" and audio not in (None, "none"):
            detail["track"] = "audio"
        elif audio == "none" and video not in (None, "none"):
            detail["track"] = "video"
        if d.get("fragment_count"):  # HLS/DASH: the source's own segments are the parts
            detail |= {"part": (d.get("fragment_index") or 0) + 1, "parts": d["fragment_count"]}
            detail["part"] = min(detail["part"], detail["parts"])
        elif d.get("connections"):
            detail["connections"] = d["connections"]
        return detail

    def retried(self, error, count: int, retries: int) -> None:
        """yt-dlp is retrying a failed request; its HTTP downloader resumes the .part file."""
        self.cb(
            self.position,
            None,
            retry=count,
            retries=retries,
            reason=describe(error),
            wait=retry_delay(count),
            kept=self.position,
        )

    def postprocessed(self, d: dict) -> None:
        stage = self.POSTPROCESSORS.get(d.get("postprocessor") or "")
        if stage and d.get("status") == "started":
            self.cb(self.done, self.done or None, stage=stage)


# ---------------------------------------------------------------- collections

# Entries yt-dlp lists in a playlist although they can't be downloaded.
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}


def collection_opts() -> dict[str, Any]:
    """Flat playlist listing: entry links and titles only, one item beyond the cap to detect it."""
    return {
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "playlistend": get_settings().collection_max_items + 1,
    }


def _title_from_url(url: str) -> str:
    slug = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").strip() or url


def collection_info(
    info: dict,
    *,
    platform: str,
    media_id: str,
    url: str,
    domains: tuple[str, ...],
    entry_url,
    audio: bool,
    thumbnail: str | None = None,
) -> MediaInfo:
    """A playlist / album / set from yt-dlp's flat listing. Entries stay links: each one is looked
    up and downloaded on its own, in order, when the user confirms. Entry links must stay on the
    platform's own domains (the network guard applies to them again when they are fetched)."""
    entries: list[CollectionEntry] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        link = entry_url(entry)
        title = " ".join(str(entry.get("title") or "").split())
        if not link or not on_domains(link, domains) or title.lower() in UNAVAILABLE_TITLES:
            continue
        duration = entry.get("duration")
        entries.append(
            CollectionEntry(
                url=link,
                title=(title or _title_from_url(link))[:200],
                duration=int(duration) if duration else None,
                uploader=entry.get("uploader") or entry.get("channel") or entry.get("artist"),
            )
        )
    if not entries:
        raise UserError("No downloadable items found in this playlist.")
    limit = get_settings().collection_max_items
    total = max(int(info.get("playlist_count") or 0), len(entries))
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    thumbs.sort(key=lambda t: t.get("width") or 0)
    return MediaInfo(
        platform=platform,
        id=media_id,
        url=url,
        title=" ".join(str(info.get("title") or "").split())[:200] or _title_from_url(url),
        uploader=info.get("uploader") or info.get("channel") or info.get("artist"),
        uploader_url=info.get("uploader_url") or info.get("channel_url"),
        thumbnail=thumbnail or info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
        description=info.get("description") or "",
        kind="collection",
        entries=entries[:limit],
        extra={"total": total, "audio": audio},
    )


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
            # JavaScript challenge solving (deno) for YouTube; empty = off. See Settings.youtube_js_runtime.
            "js_runtimes": {name: {} for name in get_settings().youtube_js_runtimes},
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
                if info.get("formats"):
                    save_streams(self.name, media_id, info)
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
            size = video.stat().st_size
            progress(size, size, stage="convert")
            mp3 = dest / "media.mp3"
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-vn",
                "-b:a",
                "192k",
                *MP3_SPEED,
                str(mp3),
            ]
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
        tracker = Progress(progress, fo.size if fo else None)
        opts: dict[str, Any] = {
            **self.ydl_opts(),
            "outtmpl": str(dest / "media.%(ext)s"),
            "format": format_selector(fo.height if fo and not audio else None, lang, audio),
            "progress_hooks": [tracker],
            "retry_hook": tracker.retried,
            # Per-request retries, resuming the .part file; `socket_timeout` is a stall timeout.
            "retries": get_settings().download_retries,
            "fragment_retries": get_settings().download_retries,
            "socket_timeout": get_settings().download_timeout,
            "retry_sleep_functions": {"http": _retry_sleep, "fragment": _retry_sleep},
            "continuedl": True,  # a re-run of the download resumes partial and finished streams
            "postprocessor_hooks": [tracker.postprocessed],
            "max_filesize": get_settings().max_source_bytes if audio else get_settings().max_file_bytes,
            # HTTP formats with a known size use the adaptive segmented downloader (parallel
            # connections, request sizes that follow the speed; see downloader.SegmentedHttpFD).
            "segmented_http": get_settings().download_connections,
            # Others (unknown size, no ranges) keep yt-dlp's own downloader in small byte ranges:
            # YouTube serves ~1 MB of a large range and then nearly stalls (measured on the VPS:
            # 200 MB ranges 0.04 MB/s, 10 MB ranges 21-23 MB/s).
            "http_chunk_size": YTDLP_CHUNK_BYTES,
            # HLS/DASH: fetch that many fragments at once.
            "concurrent_fragment_downloads": get_settings().download_connections,
        }
        if audio:
            opts |= {
                "writethumbnail": True,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
                "postprocessor_args": {"extractaudio+ffmpeg_o": MP3_SPEED},
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
        cached = load_streams(info.platform, info.id) if source == info.url else None
        with YoutubeDL(opts) as ydl:
            r = None
            if cached:
                try:
                    r = ydl.process_ie_result(cached, download=True) or {}
                except DownloadError:
                    # Expired or rejected URLs: extract again. Partial files are resumed.
                    log.info("cached stream data failed, extracting again: media=%s", info.id)
                    drop_streams(info.platform, info.id)
            if r is None:
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
