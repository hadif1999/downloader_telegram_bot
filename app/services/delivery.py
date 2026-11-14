"""Sending finished media to Telegram (shared by the bot and the worker)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)
from aiohttp import ClientConnectorError

from app.config import get_settings
from app.services import requests
from app.services.keyboards import result_kb

log = logging.getLogger(__name__)
ALBUM_SIZE = 10  # Telegram media group limit


class DeliveryNotSent(Exception):
    pass


class CheckpointError(Exception):
    """Telegram accepted a batch but Redis did not confirm the checkpoint."""


def invalid_file_id(error: TelegramBadRequest) -> bool:
    text = error.message.lower()
    return any(
        s in text
        for s in (
            "wrong file identifier",
            "wrong remote file identifier",
            "file_id",
            "file identifier",
            "file reference expired",
        )
    )


async def retry_send(call):
    for attempt in range(3):
        try:
            return await call()
        except TelegramRetryAfter as error:
            if attempt == 2 or error.retry_after > 60:
                raise DeliveryNotSent from error
            await asyncio.sleep(error.retry_after + 1)
        except TelegramNetworkError as error:
            # A read timeout may follow an accepted send. Retry only a connection that
            # provably never reached Telegram, never an ambiguous upload/response error.
            cause = error.__cause__
            if not isinstance(cause, ClientConnectorError):
                raise
            if attempt == 2:
                raise DeliveryNotSent from error
            await asyncio.sleep(2**attempt)


@dataclass
class Journal:
    request_id: str
    user_id: int
    owner: str
    sent: list = field(default_factory=list)
    total: int = 0

    @classmethod
    async def load(cls, request_id: str, user_id: int, owner: str):
        state = await asyncio.to_thread(requests.load, request_id, user_id)
        return cls(
            request_id, user_id, owner, json.loads(state.get("sent", "[]")), int(state.get("total", 0))
        )

    async def phase(self, phase: str):
        await asyncio.to_thread(
            requests.transition, self.request_id, self.user_id, self.owner, phase, self.sent, self.total
        )

    async def prepare(self, total: int):
        if self.total and self.total != total:
            raise ValueError("The post changed while delivery was in progress.")
        self.total = total
        await self.phase("working")

    async def send(self, call):
        await self.phase("sending")  # persist intent before the external side effect
        try:
            messages = await retry_send(call)
        except Exception as error:
            definite_rejection = isinstance(error, DeliveryNotSent) or (
                isinstance(error, TelegramAPIError)
                and not isinstance(error, (TelegramNetworkError, TelegramServerError))
            )
            if definite_rejection:
                await self.phase("ready")
                raise
            # Malformed/unknown responses can follow an accepted request too.
            with contextlib.suppress(Exception):
                await self.phase("uncertain")
            raise requests.DeliveryUncertain from error
        self.sent.extend(
            [["video" if m.video else "audio" if m.audio else "photo", sent_file_id(m)] for m in messages]
        )
        try:
            await self.phase("ready")
        except Exception as error:
            raise CheckpointError from error
        return messages


def local_file(path: str) -> str | InputFile:
    """With a local Bot API server the file is passed by path (no HTTP upload, up to 2 GB)."""
    s = get_settings()
    return f"file://{path}" if s.bot_api_url and s.bot_api_local else FSInputFile(path)


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
    journal: Journal | None = None,
    **extra,
) -> Message | None:
    common = {"chat_id": chat_id, "caption": caption, "reply_parameters": reply_params(reply_to)}
    if journal:
        await journal.prepare(1)
        if journal.sent:
            return None

    async def send():
        if fmt == "mp3":
            msg = await bot.send_audio(audio=media, **common, **extra)
        else:
            kb = result_kb(platform, media_id) if music_button else None
            msg = await bot.send_video(
                video=media, supports_streaming=True, reply_markup=kb, **common, **extra
            )
        return [msg]

    messages = await journal.send(send) if journal else await retry_send(send)
    return messages[0]


async def send_gallery(
    bot: Bot,
    chat_id: int,
    files: list[tuple[str | InputFile, str]],
    caption: str,
    reply_to: int | None = None,
    journal: Journal | None = None,
) -> list[Message]:
    """files: [(media, "photo" | "video")]; sent as albums of up to 10, caption on the first item."""
    if journal:
        await journal.prepare(len(files))
    sent: list[Message] = []
    offset = len(journal.sent) if journal else 0
    for start in range(offset, len(files), ALBUM_SIZE):
        group = []
        for i, (media, kind) in enumerate(files[start : start + ALBUM_SIZE]):
            cap = caption if start == 0 and i == 0 else None
            cls = InputMediaVideo if kind == "video" else InputMediaPhoto
            group.append(cls(media=media, caption=cap))

        async def send(group=group):
            if len(group) == 1:
                item = group[0]
                common = {
                    "chat_id": chat_id,
                    "caption": item.caption,
                    "reply_parameters": reply_params(reply_to),
                }
                msg = (
                    await bot.send_photo(photo=item.media, **common)
                    if isinstance(item, InputMediaPhoto)
                    else await bot.send_video(video=item.media, supports_streaming=True, **common)
                )
                return [msg]
            return await bot.send_media_group(chat_id, group, reply_parameters=reply_params(reply_to))

        sent += await journal.send(send) if journal else await retry_send(send)
    return sent


def sent_file_id(msg: Message) -> str | None:
    if msg.photo:
        return msg.photo[-1].file_id
    media = msg.video or msg.audio or msg.document
    return media.file_id if media else None


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
    journal: Journal | None = None,
) -> None:
    if fmt == "all":
        files = json.loads(cached)
        if (
            not isinstance(files, list)
            or not files
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or item[0] not in {"photo", "video"}
                or not isinstance(item[1], str)
                or not item[1]
                for item in files
            )
        ):
            raise ValueError("Invalid cached album")
        await send_gallery(bot, chat_id, [(fid, t) for t, fid in files], caption, reply_to, journal=journal)
    else:
        await send_media(
            bot,
            chat_id,
            fmt,
            cached,
            caption,
            platform,
            media_id,
            reply_to,
            music_button=kind == "video",
            journal=journal,
        )


async def edit_status(bot: Bot, chat_id: int, msg_id: int, text: str, is_caption: bool) -> None:
    try:
        if is_caption:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id, caption=text, request_timeout=5
            )
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, request_timeout=5)
    except (TelegramAPIError, TelegramNetworkError) as e:
        # "message is not modified", deleted message, flood limits, network blips:
        # a failed status edit must never abort the download itself
        log.debug("status edit failed: %s", e)
