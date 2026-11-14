"""Turning a format choice into a queued download (or an instant re-send from cache)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.config import get_settings
from app.models import MediaInfo
from app.services import cache, captions, media, requests
from app.services.delivery import CheckpointError, Journal, invalid_file_id, reply_params, send_cached
from app.services.keyboards import formats_kb

log = logging.getLogger(__name__)


async def request_download(
    bot: Bot,
    info: MediaInfo,
    fmt: str,
    lang: str | None,
    *,
    chat_id: int,
    user_id: int,
    card: Message | None = None,
    reply_to: int | None = None,
) -> str | None:
    """Queue a download. `card` (the info card) is edited into the progress message; without it a
    new status message is sent. Returns an error text for the user, or None on success."""
    if not media.valid_choice(info, fmt, lang):
        return captions.UNAVAILABLE
    fo = info.format(fmt)
    too_large = fmt != "mp3" and fo and fo.size and fo.size > get_settings().max_file_bytes
    request_id = uuid.uuid4().hex
    created = False
    publish_started = False
    try:
        from app.worker.tasks import download_media

        cached = await asyncio.to_thread(cache.get_file_id, info.platform, info.id, fmt, lang)
        if not cached and too_large:
            return captions.TOO_BIG
        created = await asyncio.to_thread(requests.create, request_id, user_id)
        if not created:
            return captions.BUSY

        if cached:
            async with requests.execution(request_id, user_id) as owner:
                if owner is None:
                    return captions.UNCERTAIN
                journal = await Journal.load(request_id, user_id, owner)
                me = await bot.me()
                caption = captions.result_caption(info, me.username)
                try:
                    await send_cached(
                        bot,
                        chat_id,
                        fmt,
                        cached,
                        caption,
                        info.platform,
                        info.id,
                        info.kind,
                        reply_to,
                        journal=journal,
                    )
                except (TelegramBadRequest, ValueError) as error:
                    if isinstance(error, TelegramBadRequest) and not invalid_file_id(error):
                        await journal.phase("failed")
                        raise
                    await asyncio.to_thread(cache.delete_file_id, info.platform, info.id, fmt, lang)
                    await journal.phase("queued")  # resume any accepted album batches in the worker
                    cached = None
                except CheckpointError:
                    if len(journal.sent) < journal.total:
                        return captions.UNCERTAIN
                    # A confirmed send is success even if Redis cannot record its result.
                    log.exception("cached delivery checkpoint failed: %s", request_id)
                    if card is not None:
                        with contextlib.suppress(Exception):
                            await card.delete()
                    return None
                else:
                    with contextlib.suppress(Exception):
                        await journal.phase("done")
                    if card is not None:
                        with contextlib.suppress(Exception):
                            await card.delete()
                    return None

        if not cached and too_large:
            await asyncio.to_thread(requests.cancel_queued, request_id, user_id)
            return captions.TOO_BIG
        if card is not None:
            if card.reply_markup is not None:
                await card.edit_reply_markup(reply_markup=None)
            text = captions.status_caption(info, captions.QUEUED)
            if card.photo:
                await card.edit_caption(caption=text)
            else:
                await card.edit_text(text)
            status_id, is_caption = card.message_id, bool(card.photo)
        else:
            status = await bot.send_message(chat_id, captions.QUEUED, reply_parameters=reply_params(reply_to))
            status_id, is_caption = status.message_id, False
        # Card edits or cached-send attempts may have taken time. Align the owned
        # queue lease with the message expiry immediately before publication.
        if not await asyncio.to_thread(requests.prepare_queue, request_id, user_id):
            raise requests.LeaseLost("Request ownership expired before publication.")
        publish_started = True
        await asyncio.to_thread(
            download_media.apply_async,
            args=(chat_id, status_id, is_caption, info.platform, info.id, fmt, lang, user_id, reply_to),
            task_id=request_id,
            expires=get_settings().queue_ttl,
            retry=False,
        )
        return None
    except requests.DeliveryUncertain:
        return captions.UNCERTAIN
    except Exception:
        log.exception("download request failed")
        if created:
            try:
                if not await asyncio.to_thread(
                    requests.cancel_queued, request_id, user_id, not publish_started
                ):
                    state = await asyncio.to_thread(requests.load, request_id, user_id)
                    if publish_started and state.get("phase") != "cancelled":
                        # A lost broker acknowledgement may follow a completed upload.
                        # Never replace the worker's outcome with a false enqueue failure.
                        return captions.UNCERTAIN if state.get("phase") in {None, "uncertain"} else None
            except Exception:
                log.exception("couldn't confirm queue cancellation: %s", request_id)
                return captions.UNCERTAIN
        if card is not None:
            with contextlib.suppress(Exception):
                kb = formats_kb(info, get_settings().max_file_bytes)
                text = captions.status_caption(info, captions.FAILED)
                if card.photo:
                    await card.edit_caption(caption=text, reply_markup=kb)
                else:
                    await card.edit_text(text, reply_markup=kb)
        return captions.FAILED
