"""Inline mode (`@bot <link>` in any chat): result lists, their Redis references, deep links.

Each format of a looked-up link becomes one result. A format whose Telegram `file_id` is cached
is a "cached" result that Telegram sends at once. Any other format is an article that posts a
status card with a Start button (the keyboard is what gives us an `inline_message_id`). When the
result is chosen, the download runs like any other request. The worker uploads to a storage
chat, then swaps the card for the media (`delivery.deliver_inline`).

Result IDs are personal (they include the user) and map to the choice in Redis, so a Start button
or a chosen result can be resolved without trusting anything but the ID.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from app.config import get_settings
from app.i18n import t
from app.models import FormatOption, MediaInfo
from app.services import cache, captions
from app.services.keyboards import inline_start_kb

LINK_TTL = 7 * 24 * 3600
MAX_RESULTS = 50  # Telegram's limit per answer
BUTTON_TEXT = 64  # keep the results button on one line
QUERY_LIMIT = 256  # longest text a switch_inline_query button may carry


def result_id(user_id: int, platform: str, media_id: str, fmt: str) -> str:
    digest = hashlib.sha1(f"{user_id}|{platform}|{media_id}|{fmt}".encode()).hexdigest()
    return digest[:24]


def _key(kind: str, value: str) -> str:
    return f"inl:{cache.namespace()}:{kind}:{value}"


def save_choices(user_id: int, info: MediaInfo, formats: list[str]) -> None:
    pipe = cache.client().pipeline()
    for fmt in formats:
        rid = result_id(user_id, info.platform, info.id, fmt)
        value = "|".join([info.platform, info.id, fmt, str(user_id)])
        pipe.set(_key("r", rid), value, ex=get_settings().queue_ttl + 3600)
    pipe.execute()


def load_choice(rid: str) -> tuple[str, str, str, int] | None:
    """(platform, media id, format, user id) of a result, or None once it has expired."""
    raw = cache.client().get(_key("r", rid))
    parts = raw.split("|") if raw else []
    if len(parts) != 4 or not parts[3].isdigit():
        return None
    return parts[0], parts[1], parts[2], int(parts[3])


def claim_start(inline_message_id: str) -> bool:
    """True once per inline message: a chosen result and its Start button never both start it."""
    ttl = get_settings().queue_ttl + 3600
    return bool(cache.client().set(_key("started", inline_message_id), "1", nx=True, ex=ttl))


def release_start(inline_message_id: str) -> None:
    """Let the Start button try again (the request was refused before it was queued)."""
    cache.client().delete(_key("started", inline_message_id))


def link_token(url: str) -> str:
    """A short deep-link token for `url` (`/start l_<token>` opens it in the private chat)."""
    token = hashlib.sha1(url.encode()).hexdigest()[:20]
    cache.client().set(_key("l", token), url, ex=LINK_TTL)
    return token


def link_for_token(token: str) -> str | None:
    return cache.client().get(_key("l", token)) if token.isalnum() else None


def bot_link(bot_username: str, payload: str = "") -> str:
    return f"https://t.me/{bot_username}" + (f"?start={payload}" if payload else "")


def preview_url(info: MediaInfo | None) -> str | None:
    """The thumbnail, if Telegram can fetch it for a link preview or result thumbnail."""
    url = info.thumbnail if info else None
    try:
        return url if url and len(url) <= 2048 and urlsplit(url).scheme in {"http", "https"} else None
    except ValueError:
        return None


def switch_query(info: MediaInfo | None) -> str | None:
    """The link to put back into the inline query field (Choose again / music buttons)."""
    url = info.url if info else ""
    return url if url and len(url) <= QUERY_LIMIT else None


def offered(info: MediaInfo, max_bytes: int) -> list[FormatOption]:
    """Formats offered as results. Playlists and albums of several items go to the bot instead."""
    if info.kind == "collection":
        return []
    if info.kind == "gallery":
        if len(info.items) != 1:
            return []
        return [FormatOption("all", _item_label(info.items[0].type), None)]
    options = [f for f in info.formats if f.key != "mp3" and (f.size is None or f.size <= max_bytes)]
    if mp3 := info.format("mp3"):
        options.append(mp3)
    return options[:MAX_RESULTS]


def _item_label(kind: str) -> str:
    return {"photo": t("🖼 Photo"), "audio": t("🎵 Track")}.get(kind, t("🎬 Video"))


def media_kind(fmt: str, item_type: str | None = None) -> str:
    """The Telegram media type a result is delivered as: video, audio or photo."""
    if fmt == "all":
        return item_type or "photo"
    return "audio" if fmt == "mp3" else "video"


def cached_media(fmt: str, cached: str | None) -> tuple[str, str] | None:
    """(media type, file_id) from a file_id cache entry; galleries store a JSON list."""
    if not cached:
        return None
    if fmt != "all":
        return media_kind(fmt), cached
    try:
        files = json.loads(cached)
        kind, file_id = files[0]
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    if len(files) != 1 or kind not in {"photo", "video", "audio"} or not isinstance(file_id, str):
        return None
    return kind, file_id


def _title(option: FormatOption, info: MediaInfo, instant: bool) -> str:
    if option.key == "all":
        label = option.label
    elif option.key == "mp3":
        label = "🎵 MP3"
    else:
        label = f"{'🎧' if info.kind == 'audio' else '🎬'} {option.label}"
    if instant:
        return t("⚡ {label} · instant", label=label)
    return f"{label} · {captions.human_size(option.size)}" if option.size else label


def _description(info: MediaInfo) -> str:
    parts = [captions._short(info.title, 60)]
    if info.uploader:
        parts.append(captions._short(info.uploader, 30))
    if info.duration:
        parts.append(captions.human_duration(info.duration))
    return " · ".join(parts)


def build_results(user_id: int, info: MediaInfo, cached: dict[str, str | None], bot_username: str):
    """One result per offered format: cached ones are sent by Telegram, others start a download."""
    results = []
    thumb = preview_url(info)
    for option in offered(info, get_settings().max_file_bytes):
        rid = result_id(user_id, info.platform, info.id, option.key)
        ready = cached_media(option.key, cached.get(option.key))
        title, description = _title(option, info, bool(ready)), _description(info)
        if ready:
            kind, file_id = ready
            caption = captions.result_caption(info, bot_username)
            if kind == "audio":
                results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=file_id, caption=caption))
            elif kind == "photo":
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=rid, photo_file_id=file_id, title=title, description=description, caption=caption
                    )
                )
            else:
                results.append(
                    InlineQueryResultCachedVideo(
                        id=rid, video_file_id=file_id, title=title, description=description, caption=caption
                    )
                )
            continue
        preview = (
            LinkPreviewOptions(url=thumb, prefer_large_media=True, show_above_text=True)
            if thumb
            else LinkPreviewOptions(is_disabled=True)
        )
        results.append(
            InlineQueryResultArticle(
                id=rid,
                title=title,
                description=description,
                thumbnail_url=thumb,
                input_message_content=InputTextMessageContent(
                    message_text=captions.status_caption(info, captions.INLINE_STARTING, option.key),
                    link_preview_options=preview,
                ),
                reply_markup=inline_start_kb(rid),
            )
        )
    return results


def more_button(info: MediaInfo, has_results: bool) -> InlineQueryResultsButton | None:
    """ "Open in the bot" above the results, for what inline mode can't offer here."""
    if info.kind == "collection":
        text = t("📂 Open the playlist in the bot")
    elif info.kind == "gallery" and len(info.items) != 1:
        text = t("📂 Open the album in the bot")
    elif any(not track.original for track in info.audio_tracks):
        text = t("🌐 More audio languages in the bot")
    elif not has_results:
        text = t("📂 More options in the bot")
    else:
        return None
    return results_button(text, "l_" + link_token(info.url))


def results_button(text: str, payload: str = "inline") -> InlineQueryResultsButton:
    return InlineQueryResultsButton(text=captions._short(text, BUTTON_TEXT), start_parameter=payload)
