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
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
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
    step: str = ""  # inline delivery progress (see requests.set_step)

    @classmethod
    async def load(cls, request_id: str, user_id: int, owner: str):
        state = await asyncio.to_thread(requests.load, request_id, user_id)
        return cls(
            request_id,
            user_id,
            owner,
            json.loads(state.get("sent", "[]")),
            int(state.get("total", 0)),
            state.get("step", ""),
        )

    async def mark(self, step: str):
        await asyncio.to_thread(requests.set_step, self.request_id, self.user_id, self.owner, step)
        self.step = step

    async def phase(self, phase: str):
        await asyncio.to_thread(
            requests.transition, self.request_id, self.user_id, self.owner, phase, self.sent, self.total
        )

    async def prepare(self, total: int):
        if self.total and self.total != total:
            raise ValueError("The post changed while delivery was in progress.")
        self.total = total
        await self.phase("working")

    async def send(self, call, expected: int = 1):
        await self.phase("sending")  # persist intent before the external side effect
        try:
            messages = await retry_send(call)
            batch = [
                ["video" if m.video else "audio" if m.audio else "photo", sent_file_id(m)] for m in messages
            ]
            if (
                len(batch) != expected
                or any(not isinstance(fid, str) or not fid for _, fid in batch)
                or len(self.sent) + len(batch) > self.total
            ):
                raise ValueError("Incomplete or invalid Telegram media response")
        except Exception as error:
            definite_rejection = isinstance(error, (DeliveryNotSent, TelegramEntityTooLarge)) or (
                isinstance(error, TelegramAPIError)
                and not isinstance(error, (TelegramNetworkError, TelegramServerError))
            )
            if definite_rejection:
                try:
                    await self.phase("ready")
                except Exception:
                    log.exception("couldn't record rejected upload: request=%s", self.request_id)
                raise
            # Malformed/unknown responses can follow an accepted request too.
            with contextlib.suppress(Exception):
                await self.phase("uncertain")
            raise requests.DeliveryUncertain from error
        self.sent.extend(batch)
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
    """Photo/video or audio-only albums of up to 10, caption on the first item."""
    kinds = {kind for _, kind in files}
    if not kinds <= {"photo", "video", "audio"} or ("audio" in kinds and len(kinds) > 1):
        raise ValueError("Audio albums cannot be mixed with photos or videos")
    if journal:
        await journal.prepare(len(files))
    sent: list[Message] = []
    offset = len(journal.sent) if journal else 0
    for start in range(offset, len(files), ALBUM_SIZE):
        group = []
        for i, (media, kind) in enumerate(files[start : start + ALBUM_SIZE]):
            cap = caption if start == 0 and i == 0 else None
            cls = {"video": InputMediaVideo, "photo": InputMediaPhoto, "audio": InputMediaAudio}[kind]
            group.append(cls(media=media, caption=cap))

        async def send(group=group):
            if len(group) == 1:
                item = group[0]
                common = {
                    "chat_id": chat_id,
                    "caption": item.caption,
                    "reply_parameters": reply_params(reply_to),
                }
                if isinstance(item, InputMediaPhoto):
                    msg = await bot.send_photo(photo=item.media, **common)
                elif isinstance(item, InputMediaAudio):
                    msg = await bot.send_audio(audio=item.media, **common)
                else:
                    msg = await bot.send_video(video=item.media, supports_streaming=True, **common)
                return [msg]
            return await bot.send_media_group(chat_id, group, reply_parameters=reply_params(reply_to))

        sent += await journal.send(send, expected=len(group)) if journal else await retry_send(send)
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
                or item[0] not in {"photo", "video", "audio"}
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


async def edit_status(
    bot: Bot,
    chat_id: int,
    msg_id: int,
    text: str,
    is_caption: bool,
    markup=None,
    *,
    inline_id: str | None = None,
    preview: str | None = None,
) -> None:
    """Edit the status card. Without `markup` any keyboard (e.g. Cancel) is removed.

    `inline_id` targets a card posted through inline mode (always text) instead of a chat message;
    `preview` keeps its thumbnail shown as a large link preview above the text."""
    target = {"inline_message_id": inline_id} if inline_id else {"chat_id": chat_id, "message_id": msg_id}
    try:
        if is_caption and not inline_id:
            await bot.edit_message_caption(**target, caption=text, reply_markup=markup, request_timeout=5)
        else:
            extra = {}
            if preview:
                extra["link_preview_options"] = LinkPreviewOptions(
                    url=preview, prefer_large_media=True, show_above_text=True
                )
            await bot.edit_message_text(text, **target, reply_markup=markup, request_timeout=5, **extra)
    except (TelegramAPIError, TimeoutError) as e:
        # "message is not modified", deleted message, flood limits, network blips:
        # a failed status edit must never abort the download itself
        log.debug("status edit failed: %s", type(e).__name__)
    except Exception:
        log.exception("unexpected status edit failure: chat=%s message=%s", chat_id, msg_id)


# --- inline mode: the card posted in someone else's chat becomes the media

INLINE_MEDIA = {"video": InputMediaVideo, "audio": InputMediaAudio, "photo": InputMediaPhoto}
INLINE_FINAL = {"edited", "pm", "start"}
DEFINITE = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, DeliveryNotSent)


async def put_inline(bot: Bot, inline_id: str, kind: str, file_id: str, caption: str, markup=None) -> None:
    """Replace an inline card with already-uploaded media (inline messages only take file_ids).

    Editing is idempotent, so unlike a send it is safe to retry after an unknown outcome: repeating
    it can never create a second message. Raises a `DEFINITE` error when Telegram refused the edit,
    or the last network error when its outcome stayed unknown."""
    extra = {"supports_streaming": True} if kind == "video" else {}
    item = INLINE_MEDIA[kind](media=file_id, caption=caption, **extra)
    for attempt in range(3):
        try:
            await bot.edit_message_media(media=item, inline_message_id=inline_id, reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return  # an earlier attempt already went through
            raise
        except TelegramRetryAfter as error:
            if attempt == 2 or error.retry_after > 60:
                raise DeliveryNotSent from error
            await asyncio.sleep(error.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)


async def _send_file(bot: Bot, chat_id: int, kind: str, file_id: str, caption: str):
    if kind == "audio":
        return await bot.send_audio(chat_id, file_id, caption=caption)
    if kind == "photo":
        return await bot.send_photo(chat_id, file_id, caption=caption)
    return await bot.send_video(chat_id, file_id, caption=caption, supports_streaming=True)


async def deliver_inline(
    bot: Bot,
    journal: Journal,
    inline_id: str,
    kind: str,
    file_id: str,
    caption: str,
    *,
    stored_in_pm: bool,
    markup=None,
    notice=None,
) -> str:
    """Finish an inline request whose file is already uploaded to the storage chat.

    1. Swap the card for the media (`edited`).
    2. If Telegram definitely refused that (deleted card, media not allowed in that chat), send the
       file to the user's private chat (`pm`), unless it is already there as the storage copy.
    3. If the bot may not message the user (they never pressed Start), say so on the card
       (`start`).
    A private-chat send whose outcome is unknown is never repeated (`pm_sending` -> uncertain), and
    neither is an edit that stayed unconfirmed. `notice(step)` edits the card for steps 2 and 3.
    Returns the final step."""
    if journal.step in INLINE_FINAL:
        return journal.step
    try:
        await put_inline(bot, inline_id, kind, file_id, caption, markup)
        step = "edited"
    except DEFINITE as refused:
        log.info("inline card refused the media (%s): request=%s", type(refused).__name__, journal.request_id)
        if stored_in_pm:
            step = "pm"
        elif journal.step == "pm_sending":
            await _uncertain(journal)  # an earlier attempt may have reached the private chat
            raise requests.DeliveryUncertain from None
        else:
            await journal.mark("pm_sending")  # persist intent before the external side effect
            try:
                await retry_send(lambda: _send_file(bot, journal.user_id, kind, file_id, caption))
                step = "pm"
            except DEFINITE:
                step = "start"
            except Exception as error:
                await _uncertain(journal)
                raise requests.DeliveryUncertain from error
        if notice is not None:
            await notice(step)
    except (TelegramNetworkError, TelegramServerError) as error:
        await _uncertain(journal)
        raise requests.DeliveryUncertain from error
    try:
        await journal.mark(step)
    except Exception:
        log.exception("couldn't record the inline delivery step: request=%s", journal.request_id)
    return step


async def _uncertain(journal: Journal) -> None:
    with contextlib.suppress(Exception):
        await journal.phase("uncertain")
