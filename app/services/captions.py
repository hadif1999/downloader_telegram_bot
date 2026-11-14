"""Message texts (HTML parse mode). Pure functions so they are easy to test."""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urlsplit

from app.models import MediaInfo

CAPTION_LIMIT = 1024  # Telegram media caption limit


def text_chunks(text: str, limit: int = 4000):
    """Telegram entities use UTF-16 offsets; never split a character or escaped entity."""
    chunk, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield "".join(chunk)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text.encode("utf-16-le")) <= limit * 2:
        return text
    return next(text_chunks(text, limit - 1)) + "…"


def _link(text: str, url: str | None) -> str:
    try:
        if (
            url
            and len(url) <= 2048
            and not any(ord(c) < 33 for c in url)
            and urlsplit(url).scheme in {"http", "https"}
        ):
            return f'<a href="{escape(url)}">{escape(text)}</a>'
    except ValueError:
        pass
    return escape(text)


def _units(html: str) -> int:
    return len(unescape(re.sub(r"<[^>]*>", "", html)).encode("utf-16-le")) // 2


def _caption(info: MediaInfo, suffix: str) -> str:
    # Keep complete HTML lines and reserve room for the status/actions. Never slice markup.
    tail = []
    for line in suffix.splitlines():
        if _units("\n".join([*tail, line])) > CAPTION_LIMIT // 2:
            break
        tail.append(line)
    suffix = "\n".join(tail)
    budget = CAPTION_LIMIT - _units(suffix) - 2
    head = []
    for line in info_header(info).splitlines():
        if _units("\n".join([*head, line])) <= budget:
            head.append(line)
    return "\n".join(head) + (f"\n\n{suffix}" if suffix else "")


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
    lines = [f"{icon} {_link(_short(info.title, 200), info.url)}"]
    if info.uploader:
        who = escape(_short(info.uploader, 100))
        if info.uploader_handle and info.uploader_url:
            who += " " + _link(_short(info.uploader_handle, 60), info.uploader_url)
        lines.append(f"👤 {who}")
    if info.subscribers is not None:
        lines.append(f"👥 {_num(info.subscribers)}")
    if info.duration:
        lines.append(f"⏱ {human_duration(info.duration)}")
    if info.views is not None:
        lines.append(f"👁 {_num(info.views)} | 👍 {_num(info.likes)} | 💬 {_num(info.comments)}")
    if info.category:
        lines.append(f"🏷 {escape(_short(info.category, 80))}")
    if info.upload_date:
        lines.append(f"📅 {escape(_short(info.upload_date, 32))}")
    return "\n".join(lines)


def format_list(info: MediaInfo, max_bytes: int) -> str:
    rows = []
    for f in info.formats:
        ok = f.size is None or f.size <= max_bytes
        rows.append(f"{'✅' if ok else '❌'} {escape(_short(f.label, 40))} - {human_size(f.size)}")
    return "\n".join(rows)


def gallery_summary(info: MediaInfo) -> str:
    photos = sum(i.type == "photo" for i in info.items)
    videos = len(info.items) - photos
    parts = [f"🖼 {photos} photo{'s' * (photos != 1)}"] if photos else []
    parts += [f"🎬 {videos} video{'s' * (videos != 1)}"] if videos else []
    return " · ".join(parts)


def info_caption(info: MediaInfo, max_bytes: int) -> str:
    body = gallery_summary(info) if info.kind == "gallery" else format_list(info, max_bytes)
    return _caption(info, f"{body}\n\nFormats for download ⤵️")


def status_caption(info: MediaInfo, status: str) -> str:
    return _caption(info, status)


QUEUED = "🕐 The selected format has been added to the download queue. Please wait…"
EXTRACT_WAITING = "🔗 Link received. Waiting to fetch media details…"


def extracting_caption(platform: str, elapsed: int = 0) -> str:
    text = f"🔎 Checking your {escape(platform)} link…\nFetching media details and available formats."
    if elapsed:
        text += f"\n⏱ {elapsed}s elapsed. Some sites take longer to respond."
    return text


UPLOADING = "📤 Uploading to Telegram…"
BUSY = "⏳ You already have a download in progress. Please wait until it finishes."
EXPIRED = "⌛ This request has expired, please send the link again."
FAILED = "⚠️ Download failed. Please try again later or pick another format."
TOO_BIG = "❌ This format is larger than the upload limit."
UNAVAILABLE = "⚠️ This format or audio language is no longer available. Please send the link again."
UNCERTAIN = "⚠️ Delivery could not be confirmed. Please check the chat before requesting it again."


def progress_text(done: int, total: int | None, cells: int = 10, unit: str = "bytes") -> str:
    if not total:
        return f"📥 Downloading…\n{human_size(done)}"
    pct = max(0, min(100, int(done * 100 / total)))
    filled = pct * cells // 100
    bar = "▓" * filled + "░" * (cells - filled)
    amount = f"{done}/{total} files" if unit == "files" else f"{human_size(done)} / {human_size(total)}"
    return f"📥 Downloading…\n{bar} {pct}%  ({amount})"


def result_caption(info: MediaInfo, bot_username: str) -> str:
    return _caption(info, f"Downloaded by @{escape(_short(bot_username, 64))} 🤖")


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
