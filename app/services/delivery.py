"""Sending finished media to Telegram (shared by the bot and the worker)."""

from __future__ import annotations

import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)

from app.config import get_settings
from app.services.keyboards import result_kb

log = logging.getLogger(__name__)
ALBUM_SIZE = 10  # Telegram media group limit


def local_file(path: str) -> str | InputFile:
    """With a local Bot API server the file is passed by path (no HTTP upload, up to 2 GB)."""
    return f"file://{path}" if get_settings().bot_api_local else FSInputFile(path)


def reply_params(reply_to: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None


async def send_media(
    bot: Bot,
    chat_id: int,
    fmt: str,
    media: str | InputFile,
    caption: str,
    platform: str,
    media_id: str,
    reply_to: int | None = None,
    music_button: bool = True,
    **extra,
) -> Message:
    common = {"chat_id": chat_id, "caption": caption, "reply_parameters": reply_params(reply_to)}
    if fmt == "mp3":
        return await bot.send_audio(audio=media, **common, **extra)
    kb = result_kb(platform, media_id) if music_button else None
    return await bot.send_video(video=media, supports_streaming=True, reply_markup=kb, **common, **extra)


async def send_gallery(
    bot: Bot,
    chat_id: int,
    files: list[tuple[str | InputFile, str]],
    caption: str,
    reply_to: int | None = None,
) -> list[Message]:
    """files: [(media, "photo" | "video")]; sent as albums of up to 10, caption on the first item."""
    sent: list[Message] = []
    for start in range(0, len(files), ALBUM_SIZE):
        group = []
        for i, (media, kind) in enumerate(files[start : start + ALBUM_SIZE]):
            cap = caption if start == 0 and i == 0 else None
            cls = InputMediaVideo if kind == "video" else InputMediaPhoto
            group.append(cls(media=media, caption=cap))
        sent += await bot.send_media_group(chat_id, group, reply_parameters=reply_params(reply_to))
    return sent


def sent_file_id(msg: Message) -> str | None:
    if msg.photo:
        return msg.photo[-1].file_id
    media = msg.video or msg.audio or msg.document
    return media.file_id if media else None


def gallery_file_ids(messages: list[Message]) -> str:
    return json.dumps([["video" if m.video else "photo", sent_file_id(m)] for m in messages])


async def send_cached(
    bot: Bot,
    chat_id: int,
    fmt: str,
    cached: str,
    caption: str,
    platform: str,
    media_id: str,
    kind: str,
    reply_to,
) -> None:
    if fmt == "all":
        await send_gallery(bot, chat_id, [(fid, t) for t, fid in json.loads(cached)], caption, reply_to)
    else:
        await send_media(
            bot, chat_id, fmt, cached, caption, platform, media_id, reply_to, music_button=kind == "video"
        )


async def edit_status(bot: Bot, chat_id: int, msg_id: int, text: str, is_caption: bool) -> None:
    try:
        if is_caption:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
    except (TelegramAPIError, TelegramNetworkError) as e:
        # "message is not modified", deleted message, flood limits, network blips:
        # a failed status edit must never abort the download itself
        log.debug("status edit failed: %s", e)
