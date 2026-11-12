"""Sending finished media to Telegram (shared by the bot and the worker)."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputFile, Message, ReplyParameters

from app.config import get_settings
from app.services.keyboards import result_kb


def local_file(path: str) -> str | InputFile:
    """With a local Bot API server the file is passed by path (no HTTP upload, up to 2 GB)."""
    return f"file://{path}" if get_settings().bot_api_local else FSInputFile(path)


async def send_media(
    bot: Bot,
    chat_id: int,
    fmt: str,
    media: str | InputFile,
    caption: str,
    platform: str,
    media_id: str,
    reply_to: int | None = None,
    **extra,
) -> Message:
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    common = {"chat_id": chat_id, "caption": caption, "reply_parameters": reply}
    if fmt == "mp3":
        return await bot.send_audio(audio=media, **common, **extra)
    return await bot.send_video(
        video=media, supports_streaming=True, reply_markup=result_kb(platform, media_id), **common, **extra
    )


def sent_file_id(msg: Message) -> str | None:
    media = msg.video or msg.audio or msg.document
    return media.file_id if media else None


async def edit_status(bot: Bot, chat_id: int, msg_id: int, text: str, is_caption: bool) -> None:
    try:
        if is_caption:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
    except TelegramBadRequest:
        pass  # "message is not modified" / deleted by user
