"""Turning a format choice into a queued download (or an instant re-send from cache)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Message

from app.config import get_settings
from app.models import MediaInfo
from app.services import cache, captions
from app.services.delivery import reply_params, send_cached

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
    me = await bot.me()
    if cached := cache.get_file_id(info.platform, info.id, fmt, lang):
        caption = captions.result_caption(info, me.username)
        await send_cached(bot, chat_id, fmt, cached, caption, info.platform, info.id, info.kind, reply_to)
        return None

    fo = info.format(fmt)
    if fo and fo.size and fo.size > get_settings().max_file_bytes:
        return captions.TOO_BIG
    if not cache.acquire_user_lock(user_id):
        return captions.BUSY

    try:
        from app.worker.tasks import download_media

        if card is not None:
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
        download_media.delay(
            chat_id, status_id, is_caption, info.platform, info.id, fmt, lang, user_id, reply_to
        )
        return None
    except Exception:
        cache.release_user_lock(user_id)
        log.exception("enqueue failed")
        return captions.FAILED
