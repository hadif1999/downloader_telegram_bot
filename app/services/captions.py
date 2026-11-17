"""Message texts (HTML parse mode). Pure functions so they are easy to test."""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urlsplit

from app.i18n import t
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


def _caption(info: MediaInfo, suffix: str, *, header: str | None = None) -> str:
    # Keep complete HTML lines and reserve room for the status/actions. Never slice markup.
    tail = []
    for line in suffix.splitlines():
        if _units("\n".join([*tail, line])) > CAPTION_LIMIT // 2:
            break
        tail.append(line)
    suffix = "\n".join(tail)
    budget = CAPTION_LIMIT - _units(suffix) - 2
    head = []
    for line in (info_header(info) if header is None else header).splitlines():
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
    if info.items and all(i.type == "audio" for i in info.items):
        icon = "🎵"
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
    videos = sum(i.type == "video" for i in info.items)
    tracks = sum(i.type == "audio" for i in info.items)
    parts = [t("🖼 {n} photo" if photos == 1 else "🖼 {n} photos", n=photos)] if photos else []
    parts += [t("🎬 {n} video" if videos == 1 else "🎬 {n} videos", n=videos)] if videos else []
    parts += [t("🎵 {n} track" if tracks == 1 else "🎵 {n} tracks", n=tracks)] if tracks else []
    return " · ".join(parts)


def info_caption(info: MediaInfo, max_bytes: int) -> str:
    body = gallery_summary(info) if info.kind == "gallery" else format_list(info, max_bytes)
    return _caption(info, f"{body}\n\n" + t("Formats for download ⤵️"))


def status_caption(info: MediaInfo, status: str, fmt: str | None = None, lang: str | None = None) -> str:
    """Put the active stage first, with only the media context needed during a download."""
    details = []
    if info.uploader:
        details.append(_short(info.uploader, 50))
    if fmt and (option := info.format(fmt)):
        details.append(_short(option.label, 40))
    elif fmt == "all" and info.items:
        details.append(t("{n} files", n=len(info.items)))
    if lang:
        details.append(_short(lang.upper(), 20))
    if info.duration:
        details.append(human_duration(info.duration))
    context = _link(_short(info.title, 140), info.url)
    if details:
        context += "\n<i>" + escape(" · ".join(details)) + "</i>"
    return _caption(info, context, header=status)


# Fixed status texts, in English. Read them as module attributes (captions.BUSY): each read
# returns the text in the current language (see __getattr__ below).
TEXTS = {
    "QUEUED": "🕓 <b>Queued</b>\nWaiting for a download slot…",
    "EXTRACT_WAITING": "🔗 Link received. Waiting to fetch media details…",
    "UPLOADING": "📤 <b>Uploading to Telegram</b>\nDownload complete. Sending your file…",
    "CANCELLED": "🚫 <b>Download cancelled</b>\nNothing was sent. Pick a format below to start again.",
    "CANCEL_DONE": "🚫 Download cancelled.",
    "CANCEL_PENDING": "🛑 Cancelling… the download stops within a few seconds.",
    "CANCEL_TOO_LATE": "📤 Too late to cancel: the file is already being sent.",
    "CANCEL_DENIED": "Only the person who started this download can cancel it.",
    "BUSY": "⏳ You already have a download in progress. Please wait until it finishes.",
    "EXPIRED": "⌛ This request has expired, please send the link again.",
    "FAILED": "⚠️ Download failed. Please try again later or pick another format.",
    "TOO_BIG": "❌ This format is larger than the upload limit.",
    "UNAVAILABLE": "⚠️ This format or audio language is no longer available. Please send the link again.",
    "UNCERTAIN": "⚠️ Delivery could not be confirmed. Please check the chat before requesting it again.",
    "JOIN_REQUIRED": "Join the channel first, then check your membership.",
    "JOIN_NOT_FOUND": "Membership not found yet. Join the channel, then check again.",
    "JOIN_UNAVAILABLE": "Membership verification is temporarily unavailable. Please try again.",
    "JOIN_REQUIRED_MANY": "Join all of these channels first, then check your membership.",
    "JOIN_NOT_FOUND_MANY": "Membership not found in every channel yet. Join the remaining channels, then check again.",
}


def __getattr__(name: str) -> str:
    if name in TEXTS:
        return t(TEXTS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def extracting_caption(platform: str, elapsed: int = 0) -> str:
    text = t(
        "🔎 Checking your {platform} link…\nFetching media details and available formats.",
        platform=escape(platform),
    )
    if elapsed:
        text += "\n" + t("⏱ {elapsed}s elapsed. Some sites take longer to respond.", elapsed=elapsed)
    return text


def force_join_caption(titles: str | list[str], notice: str | None = None) -> str:
    """Prompt for the channels the user still has to join (one or several)."""
    titles = [titles] if isinstance(titles, str) else list(titles)
    notice = escape(notice if notice is not None else t(TEXTS["JOIN_REQUIRED"]))
    if len(titles) == 1:
        title = escape(_short(titles[0], 100))
        return t("🔒 <b>Join our channel</b>\n\nTo use this bot, join <b>{title}</b>.", title=title) + (
            f"\n\n{notice}"
        )
    listed = "\n".join(f"• <b>{escape(_short(title, 100))}</b>" for title in titles)
    return t("🔒 <b>Join our channels</b>\n\nTo use this bot, join:") + f"\n{listed}\n\n{notice}"


def result_caption(info: MediaInfo, bot_username: str) -> str:
    return _caption(info, t("Downloaded by @{bot} 🤖", bot=escape(_short(bot_username, 64))))


def language_hint() -> str:
    return t("🌐 To change the language, send /language")


def welcome(platforms, bot_username: str) -> str:
    lines = [t("👋 Hello!"), "", t("📥 I can help you download videos, photos and music from:"), ""]
    lines += [f"{p.emoji} {p.title}" for p in platforms]
    lines += [
        "",
        t("• To download, just send me a link to the video, photo or track."),
        "",
        t("<i>(I also work in groups. To add me to a group, press the button below 👇)</i>"),
        "",
        language_hint(),
    ]
    return "\n".join(lines)


def language_menu(current: str) -> str:
    """Shown by /language, in both languages so anyone can find their own."""
    from app.i18n import LANGUAGES, label

    return "🌐 <b>زبان ربات را انتخاب کنید</b>\n🌐 <b>Choose the bot's language</b>\n\n" + t(
        "Current language: {language}", language=label(current if current in LANGUAGES else "fa")
    )


def language_changed() -> str:
    return t("✅ Language changed to English.") + "\n\n" + language_hint()


# --- batches: playlists, albums, sets and messages with several links

BATCH_LIST_LINES = 10  # items listed on the summary card


def _plain(html: str, limit: int) -> str:
    """A status text (may contain HTML) as a short escaped line."""
    return escape(_short(unescape(re.sub(r"<[^>]*>", "", html)), limit))


def _counts(batch) -> str:
    """Item counts by kind: '🎬 3 videos · 🎵 2 tracks'."""
    videos = sum(i.kind == "video" for i in batch.items)
    tracks = sum(i.kind == "audio" for i in batch.items)
    posts = sum(i.kind == "gallery" for i in batch.items)
    parts = [t("🎬 {n} video" if videos == 1 else "🎬 {n} videos", n=videos)] if videos else []
    parts += [t("🎵 {n} track" if tracks == 1 else "🎵 {n} tracks", n=tracks)] if tracks else []
    parts += [t("🖼 {n} post" if posts == 1 else "🖼 {n} posts", n=posts)] if posts else []
    seconds = sum(i.duration or 0 for i in batch.items)
    if seconds:
        parts.append(f"⏱ {human_duration(seconds)}")
    return " · ".join(parts)


def batch_title(batch) -> str:
    meta = batch.meta
    if meta.get("kind") == "collection":
        icon = "💿" if meta.get("audio") else "📃"
        return f"{icon} <b>{escape(_short(meta.get('title') or '', 120))}</b>"
    return t("🧺 <b>{n} links</b>", n=meta.get("links", len(batch.items)))


def batch_header(batch) -> str:
    meta = batch.meta
    lines = [batch_title(batch)]
    if meta.get("kind") == "collection":
        source = f"{meta.get('emoji', '')} {escape(meta.get('source', ''))}".strip()
        if meta.get("uploader"):
            source += f" · 👤 {escape(_short(meta['uploader'], 60))}"
        lines.append(source)
    lines.append(_counts(batch))
    if meta.get("total", 0) > len(batch.items) and meta.get("kind") == "collection":
        lines.append(
            t(
                "<i>Only the first {shown} of {total} items are offered.</i>",
                shown=len(batch.items),
                total=meta["total"],
            )
        )
    return "\n".join(line for line in lines if line)


def batch_item_line(index: int, item, selected: bool = True) -> str:
    duration = f" · {human_duration(item.duration)}" if item.duration else ""
    line = f"{index + 1}. {escape(_short(item.title, 60))}{duration}"
    return line if selected else f"<s>{line}</s>"


def quality_label(preset: str) -> str:
    return "🎵 MP3" if preset == "mp3" else f"{preset}p"


def batch_summary(batch) -> str:
    meta = batch.meta
    lines = [batch_header(batch), ""]
    for index, item in enumerate(batch.items[:BATCH_LIST_LINES]):
        lines.append(batch_item_line(index, item, batch.selected[index : index + 1] == "1"))
    if len(batch.items) > BATCH_LIST_LINES:
        lines.append(t("…and {n} more", n=len(batch.items) - BATCH_LIST_LINES))
    failed = meta.get("failed") or []
    if failed:
        lines += [
            "",
            t(
                "⚠️ Couldn't read {n} link:" if len(failed) == 1 else "⚠️ Couldn't read {n} links:",
                n=len(failed),
            ),
        ]
        lines += [f"• {escape(_short(label, 50))}: {_plain(reason, 120)}" for label, reason in failed[:10]]
    if meta.get("skipped"):
        lines += ["", t("🔗 Skipped {n} other link(s): not supported, or over the limit.", n=meta["skipped"])]
    selected = len(batch.chosen)
    status = t("✅ <b>{n}</b> of {total} selected", n=selected, total=len(batch.items))
    if not meta.get("audio_only"):
        status += " · " + t("Quality: {quality}", quality=quality_label(batch.quality))
    lines += ["", status, t("They are downloaded and sent one by one. Confirm below ⤵️")]
    return "\n".join(lines)


def batch_picker(batch, page: int, pages: int) -> str:
    return "\n".join(
        [
            batch_title(batch),
            "",
            t("Tap an item to include it or leave it out."),
            t(
                "Page {page} of {pages} · {n} of {total} selected",
                page=page + 1,
                pages=pages,
                n=len(batch.chosen),
                total=len(batch.items),
            ),
        ]
    )


def _tally(batch) -> str:
    from app.services import batches

    sent = batch.count(batches.SENT, batches.CACHED)
    parts = [f"✅ {sent}", f"⚠️ {batch.count(batches.FAILED)}", f"⏭ {batch.count(batches.SKIPPED)}"]
    if uncertain := batch.count(batches.UNCERTAIN):
        parts.append(f"❔ {uncertain}")
    return " · ".join(parts)


def batch_progress(batch, current: int | None = None) -> str:
    from app.services.progress import bar

    chosen = batch.chosen
    total = len(chosen) or 1
    finished = sum(1 for i in chosen if i in batch.results)
    position = chosen.index(current) + 1 if current in chosen else min(finished + 1, len(chosen))
    lines = [
        batch_title(batch),
        "",
        t("▶️ <b>Item {n} of {total}</b>", n=position, total=len(chosen)),
        f"<code>{bar(finished / total)}</code> {finished * 100 // total}%",
        _tally(batch),
    ]
    if current is not None and 0 <= current < len(batch.items):
        lines.append(t("Now: {title}", title=escape(_short(batch.items[current].title, 80))))
    if batch.phase == "stopping":
        lines.append(t("⏹ Stopping after the current item…"))
    lines += ["", t("<i>Each item shows its own progress card below.</i>")]
    return "\n".join(lines)


def batch_done(batch) -> str:
    from app.services import batches

    chosen = batch.chosen
    sent = batch.count(batches.SENT, batches.CACHED)
    if batch.phase == "stopped":
        status = t("⏹ <b>Stopped</b>: {sent} of {total} sent", sent=sent, total=len(chosen))
    else:
        status = t("✅ <b>Done</b>: {sent} of {total} sent", sent=sent, total=len(chosen))
    lines = [batch_title(batch), "", status, _tally(batch)]
    problems = [
        (i, code, reason)
        for i, (code, reason) in sorted(batch.results.items())
        if code in (batches.FAILED, batches.SKIPPED, batches.UNCERTAIN)
    ]
    if problems:
        lines.append("")
        for index, _code, reason in problems[:15]:
            why = _plain(reason, 80) if reason else t("cancelled")
            title = escape(_short(batch.items[index].title, 50)) if index < len(batch.items) else "?"
            lines.append(f"• {index + 1}. {title}: {why}")
        if len(problems) > 15:
            lines.append(t("…and {n} more", n=len(problems) - 15))
    if not_started := sum(1 for i in chosen if i not in batch.results):
        lines.append(t("{n} item(s) were not started.", n=not_started))
    return "\n".join(lines)
