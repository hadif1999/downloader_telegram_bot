"""Message texts (HTML parse mode). Pure functions so they are easy to test."""

from __future__ import annotations

from html import escape

from app.models import MediaInfo

CAPTION_LIMIT = 1024  # Telegram media caption limit


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return "?"


def human_duration(sec: int | None) -> str:
    if not sec:
        return "?"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _num(n: int | None) -> str:
    return f"{n:,}" if n is not None else "—"


def info_header(info: MediaInfo) -> str:
    icon = {"audio": "🎵", "gallery": "🖼"}.get(info.kind, "📹")
    lines = [f'{icon} <a href="{escape(info.url)}">{escape(info.title)}</a>']
    if info.uploader:
        who = escape(info.uploader)
        if info.uploader_handle and info.uploader_url:
            who += f' <a href="{escape(info.uploader_url)}">{escape(info.uploader_handle)}</a>'
        lines.append(f"👤 {who}")
    if info.subscribers is not None:
        lines.append(f"👥 {_num(info.subscribers)}")
    if info.duration:
        lines.append(f"⏱ {human_duration(info.duration)}")
    if info.views is not None:
        lines.append(f"👁 {_num(info.views)} | 👍 {_num(info.likes)} | 💬 {_num(info.comments)}")
    if info.category:
        lines.append(f"🏷 {escape(info.category)}")
    if info.upload_date:
        lines.append(f"📅 {info.upload_date}")
    return "\n".join(lines)


def format_list(info: MediaInfo, max_bytes: int) -> str:
    rows = []
    for f in info.formats:
        ok = f.size is None or f.size <= max_bytes
        rows.append(f"{'✅' if ok else '❌'} {f.label} - {human_size(f.size)}")
    return "\n".join(rows)


def gallery_summary(info: MediaInfo) -> str:
    photos = sum(i.type == "photo" for i in info.items)
    videos = len(info.items) - photos
    parts = [f"🖼 {photos} photo{'s' * (photos != 1)}"] if photos else []
    parts += [f"🎬 {videos} video{'s' * (videos != 1)}"] if videos else []
    return " · ".join(parts)


def info_caption(info: MediaInfo, max_bytes: int) -> str:
    body = gallery_summary(info) if info.kind == "gallery" else format_list(info, max_bytes)
    text = f"{info_header(info)}\n\n{body}\n\nFormats for download ⤵️"
    return text if len(text) <= CAPTION_LIMIT else info_header(info)[:CAPTION_LIMIT]


def status_caption(info: MediaInfo, status: str) -> str:
    return f"{info_header(info)}\n\n{status}"


QUEUED = "🕐 The selected format has been added to the download queue. Please wait…"
UPLOADING = "📤 Uploading to Telegram…"
BUSY = "⏳ You already have a download in progress. Please wait until it finishes."
EXPIRED = "⌛ This request has expired, please send the link again."
FAILED = "⚠️ Download failed. Please try again later or pick another format."
TOO_BIG = "❌ This format is larger than the upload limit."


def progress_text(done: int, total: int | None, cells: int = 10, unit: str = "bytes") -> str:
    if not total:
        return f"📥 Downloading…\n{human_size(done)}"
    pct = max(0, min(100, int(done * 100 / total)))
    filled = pct * cells // 100
    bar = "▓" * filled + "░" * (cells - filled)
    amount = f"{done}/{total} files" if unit == "files" else f"{human_size(done)} / {human_size(total)}"
    return f"📥 Downloading…\n{bar} {pct}%  ({amount})"


def result_caption(info: MediaInfo, bot_username: str) -> str:
    return f"{info_header(info)}\n\nDownloaded by @{escape(bot_username)} 🤖"


def welcome(platforms, bot_username: str) -> str:
    lines = ["👋 Hello!", "", "📥 I can help you download videos, photos and music from:", ""]
    lines += [f"{p.emoji} {p.title}" for p in platforms]
    lines += [
        "",
        "• To download, just send me a link to the video, photo or track.",
        "",
        "<i>(I also work in groups. To add me to a group, press the button below 👇)</i>",
    ]
    return "\n".join(lines)
