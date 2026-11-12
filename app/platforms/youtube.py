"""YouTube support via yt-dlp."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from app.config import get_settings
from app.models import AudioTrack, DownloadResult, FormatOption, MediaInfo
from app.platforms.base import ProgressCb

ID_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([\w-]{11})",
    re.I,
)
STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


def _size(f: dict) -> int | None:
    return f.get("filesize") or f.get("filesize_approx")


def _quality(f: dict) -> int | None:
    """Resolution class ("1080" in "1080p"), using the short side so vertical videos work."""
    w, h = f.get("width"), f.get("height")
    if not h:
        return None
    side = min(w, h) if w else h
    return min(STANDARD_HEIGHTS, key=lambda s: abs(s - side))


def _is_original_audio(f: dict) -> bool:
    note = (f.get("format_note") or "").lower()
    return "original" in note or (f.get("language_preference") or 0) >= 10


def _audio_formats(formats: list[dict]) -> list[dict]:
    return [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]


def _best_audio_size(formats: list[dict]) -> int:
    audio = _audio_formats(formats)
    m4a = [f for f in audio if f.get("ext") == "m4a"] or audio
    original = [f for f in m4a if _is_original_audio(f)] or m4a
    sizes = [_size(f) for f in original if _size(f)]
    return max(sizes) if sizes else 0


def build_formats(info: dict) -> list[FormatOption]:
    formats = info.get("formats") or []
    audio_size = _best_audio_size(formats)
    groups: dict[int, list[dict]] = {}
    for f in formats:
        if f.get("vcodec") in (None, "none") or f.get("acodec") not in (None, "none"):
            continue  # only video-only (DASH) streams
        q = _quality(f)
        if q is not None and _size(f):
            groups.setdefault(q, []).append(f)
    out = []
    for q in sorted(groups, reverse=True):
        fs = groups[q]
        # the download prefers h264 (Telegram-friendly), so estimate with it when present
        avc = [f for f in fs if (f.get("vcodec") or "").startswith("avc")] or fs
        size = max(_size(f) for f in avc) + audio_size
        out.append(FormatOption(str(q), f"{q}p", size, max(f["height"] for f in fs)))
    if audio_size:
        out.append(FormatOption("mp3", "MP3", audio_size))
    return out


def build_audio_tracks(info: dict) -> list[AudioTrack]:
    langs: dict[str, bool] = {}
    for f in _audio_formats(info.get("formats") or []):
        lang = f.get("language")
        if lang:
            langs[lang] = langs.get(lang, False) or _is_original_audio(f)
    if len(langs) < 2:
        return []
    return [
        AudioTrack(lang=lang, label=lang.upper(), original=orig)
        for lang, orig in sorted(langs.items(), key=lambda kv: (not kv[1], kv[0]))
    ]


def best_thumbnail(info: dict) -> str | None:
    """Highest-preference JPEG thumbnail (Telegram can't always fetch webp by URL)."""
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    jpgs = [t for t in thumbs if ".jpg" in t["url"]]
    best = max(jpgs or thumbs, key=lambda t: (t.get("preference") or 0, t.get("width") or 0), default=None)
    return best["url"] if best else info.get("thumbnail")


def _date(d: str | None) -> str | None:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d and len(d) == 8 else d


def to_media_info(info: dict) -> MediaInfo:
    handle = info.get("uploader_id")
    return MediaInfo(
        platform="yt",
        id=info["id"],
        url=info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}",
        title=info.get("title") or info["id"],
        uploader=info.get("channel") or info.get("uploader"),
        uploader_url=info.get("uploader_url") or info.get("channel_url"),
        uploader_handle=handle if handle and handle.startswith("@") else None,
        subscribers=info.get("channel_follower_count"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        likes=info.get("like_count"),
        comments=info.get("comment_count"),
        category=(info.get("categories") or [None])[0],
        upload_date=_date(info.get("upload_date")),
        thumbnail=best_thumbnail(info),
        description=info.get("description") or "",
        formats=build_formats(info),
        audio_tracks=build_audio_tracks(info),
    )


def format_selector(height: int | None, lang: str | None) -> str:
    lang_f = f"[language={lang}]" if lang else ""
    if height is None:  # audio only
        return f"ba[ext=m4a]{lang_f}/ba{lang_f}/ba"
    h = f"[height<={height}]"
    return (
        f"bv*{h}[ext=mp4]+ba[ext=m4a]{lang_f}/bv*{h}+ba{lang_f}/bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}/b"
    )


class _Progress:
    """Merges yt-dlp per-file progress (video + audio streams) into one counter."""

    def __init__(self, cb: ProgressCb, expected: int | None):
        self.cb, self.expected, self.done, self._seen = cb, expected, 0, set()

    def __call__(self, d: dict) -> None:
        name = d.get("filename") or d.get("tmpfilename")
        if d["status"] == "downloading":
            cur = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            grand = self.expected or (self.done + total if total else None)
            self.cb(self.done + cur, max(grand, self.done + cur) if grand else None)
        elif d["status"] == "finished" and name not in self._seen:
            self._seen.add(name)
            self.done += d.get("downloaded_bytes") or d.get("total_bytes") or 0


class TooLarge(Exception):
    pass


class YouTube:
    name = "yt"
    title = "YouTube"
    emoji = "▶️"
    implemented = True

    def match(self, url: str) -> str | None:
        m = ID_RE.search(url)
        return m.group(1) if m else None

    def url_for(self, media_id: str) -> str:
        return f"https://www.youtube.com/watch?v={media_id}"

    def _base_opts(self) -> dict[str, Any]:
        s = get_settings()
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
        }
        if s.ytdlp_cookies and Path(s.ytdlp_cookies).exists():
            opts["cookiefile"] = str(s.ytdlp_cookies)
        if s.pot_provider_url:
            opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [s.pot_provider_url]}}
        return opts

    def extract(self, url: str) -> MediaInfo:
        with YoutubeDL({**self._base_opts(), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return to_media_info(info)

    def download(
        self, info: MediaInfo, fmt: str, lang: str | None, dest: Path, progress: ProgressCb
    ) -> DownloadResult:
        fo = info.format(fmt)
        audio = fmt == "mp3"
        opts: dict[str, Any] = {
            **self._base_opts(),
            "outtmpl": str(dest / "%(id)s.%(ext)s"),
            "format": format_selector(None if audio else (fo.height if fo else None), lang),
            "progress_hooks": [_Progress(progress, fo.size if fo else None)],
            "max_filesize": get_settings().max_file_bytes,
        }
        if audio:
            opts |= {
                "writethumbnail": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    },
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
            }
        else:
            opts |= {
                "merge_output_format": "mp4",
                "format_sort": ["res", "vcodec:h264", "acodec:aac"],
            }
        with YoutubeDL(opts) as ydl:
            r = ydl.extract_info(info.url, download=True)
        if not r.get("requested_downloads"):  # skipped by max_filesize
            raise TooLarge(info.id)
        fp = r["requested_downloads"][0]["filepath"]
        return DownloadResult(
            path=fp,
            width=r.get("width"),
            height=r.get("height"),
            duration=int(r["duration"]) if r.get("duration") else None,
            title=r.get("title"),
            performer=r.get("channel") or r.get("uploader"),
        )
