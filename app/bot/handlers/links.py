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
from app.services import cache, captions
from app.services.keyboards import formats_kb

log = logging.getLogger(__name__)
router = Router(name="links")

# limit concurrent metadata extractions inside the bot process
_extract_sem = asyncio.Semaphore(4)


async def get_info(platform, media_id: str, url: str) -> MediaInfo:
    info = cache.load_info(platform.name, media_id)
    if info is None:
        async with _extract_sem:
            info = await asyncio.to_thread(platform.extract, url)
        cache.save_info(info)
    return info


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
    private = message.chat.type == ChatType.PRIVATE
    if not found:
        if private:
            await message.reply("🔗 Send me a link to a video, e.g. a YouTube link.")
        return
    platform, media_id, url = found
    if not platform.implemented:
        await message.reply(f"{platform.emoji} {platform.title} support is coming soon.")
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        info = await get_info(platform, media_id, url)
    except Exception:
        log.exception("extract failed: %s", url)
        await message.reply("⚠️ Couldn't get information about this link. Is the video public?")
        return
    await send_info_card(message, info)
