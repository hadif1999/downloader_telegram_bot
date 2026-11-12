from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.config import get_settings
from app.models import MediaInfo
from app.platforms import registry
from app.platforms.ytdlp import UserError
from app.services import captions, media
from app.services.jobs import request_download
from app.services.keyboards import formats_kb

log = logging.getLogger(__name__)
router = Router(name="links")

# limit concurrent metadata extractions inside the bot process
_extract_sem = asyncio.Semaphore(4)


async def send_info_card(message: Message, info: MediaInfo) -> None:
    max_bytes = get_settings().max_file_bytes
    caption = captions.info_caption(info, max_bytes)
    kb = formats_kb(info, max_bytes)
    if info.thumbnail:
        try:
            await message.reply_photo(info.thumbnail, caption=caption, reply_markup=kb)
            return
        except TelegramBadRequest:
            log.warning("thumbnail rejected: %s", info.thumbnail)
    await message.reply(caption, reply_markup=kb)


@router.message(F.text | F.caption)
async def on_link(message: Message) -> None:
    found = registry.detect(message.text or message.caption or "")
    if not found:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("🔗 Send me a link to a video, photo or track.")
        return
    platform, media_id, url = found
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        async with _extract_sem:
            info = await asyncio.to_thread(media.resolve, platform.name, media_id, url)
    except UserError as e:
        await message.reply(f"⚠️ {e}")
        return
    except Exception:
        log.exception("extract failed: %s", url)
        await message.reply(f"⚠️ Couldn't get this {platform.title} post. Is it public?")
        return

    if fmt := media.single_choice(info):  # nothing to choose: start right away
        error = await request_download(
            message.bot,
            info,
            fmt,
            None,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            reply_to=message.message_id,
        )
        if error:
            await message.reply(error)
        return
    await send_info_card(message, info)
