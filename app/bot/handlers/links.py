from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto, Message

from app.config import get_settings
from app.models import MediaInfo
from app.platforms import registry
from app.platforms.ytdlp import UserError
from app.services import captions, media
from app.services.delivery import edit_status
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
            await message.edit_media(InputMediaPhoto(media=info.thumbnail, caption=caption), reply_markup=kb)
            return
        except TelegramBadRequest:
            log.warning("thumbnail rejected: %s", info.thumbnail)
    await message.edit_text(caption, reply_markup=kb)


async def extraction_progress(status: Message, platform: str) -> None:
    started = asyncio.get_running_loop().time()
    while True:
        await asyncio.sleep(5)
        elapsed = int(asyncio.get_running_loop().time() - started)
        await edit_status(
            status.bot,
            status.chat.id,
            status.message_id,
            captions.extracting_caption(platform, elapsed),
            False,
        )


@router.message(F.text | F.caption)
async def on_link(message: Message) -> None:
    found = registry.detect(message.text or message.caption or "")
    if not found:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("🔗 Send me a link to a video, photo or track.")
        return
    platform, media_id, url = found
    status = await message.reply(captions.EXTRACT_WAITING)
    try:
        async with _extract_sem:
            await edit_status(
                message.bot,
                status.chat.id,
                status.message_id,
                captions.extracting_caption(platform.title),
                False,
            )
            progress = asyncio.create_task(extraction_progress(status, platform.title))
            try:
                info = await asyncio.to_thread(media.resolve, platform.name, media_id, url)
            finally:
                progress.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress
    except UserError as e:
        await status.edit_text(f"⚠️ {e}")
        return
    except Exception:
        log.exception("extract failed: %s", url)
        await status.edit_text(f"⚠️ Couldn't get this {platform.title} post. Is it public?")
        return

    try:
        if fmt := media.single_choice(info):  # nothing to choose: start right away
            error = await request_download(
                message.bot,
                info,
                fmt,
                None,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                card=status,
                reply_to=message.message_id,
            )
            if error:
                await status.edit_text(error)
            return
        await send_info_card(status, info)
    except Exception:
        log.exception("couldn't prepare media: %s", url)
        await status.edit_text(captions.FAILED)
