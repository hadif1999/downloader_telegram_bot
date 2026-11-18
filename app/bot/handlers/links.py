from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto, Message

from app.bot.errors import report_error
from app.bot.handlers import batch
from app.config import get_settings
from app.i18n import t
from app.models import MediaInfo
from app.platforms import registry
from app.services import captions, media
from app.services.delivery import edit_status
from app.services.jobs import request_download
from app.services.keyboards import formats_kb

log = logging.getLogger(__name__)
router = Router(name="links")

# limit concurrent metadata extractions inside the bot process
_extract_sem = media.extraction_slots


async def send_info_card(message: Message, info: MediaInfo) -> None:
    max_bytes = get_settings().max_file_bytes
    caption = captions.info_caption(info, max_bytes)
    kb = formats_kb(info, max_bytes)
    if info.thumbnail:
        try:
            async with media.thumbnail_file(info.thumbnail) as thumbnail:
                await message.edit_media(InputMediaPhoto(media=thumbnail, caption=caption), reply_markup=kb)
            return
        except (TelegramBadRequest, OSError, ValueError):
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
    await handle_links(message, message.text or message.caption or "")


async def handle_links(message: Message, text: str) -> None:
    """The links in `text` (one: format card; several: batch card), answered under `message`.
    Also reached from `/start l_<token>`, the "open in the bot" deep link of inline mode."""
    found, skipped = registry.detect_all(text, get_settings().message_max_links)
    if not found:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply(
                t("🔗 Send me a link to a video, photo or track.") + "\n\n" + captions.language_hint()
            )
        return
    if message.from_user is None:
        return
    status = await message.reply(captions.EXTRACT_WAITING)
    if len(found) > 1:  # several links: one summary card, then one download after another
        try:
            await batch.lookup_many(message, status, found, skipped)
        except Exception as error:
            log.exception("batch lookup failed")
            await report_error(status, error, edit=True)
        return
    platform, media_id, url = found[0]
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
                    try:
                        await progress
                    except Exception:
                        log.exception(
                            "extraction progress failed: platform=%s media=%s", platform.name, media_id
                        )
    except Exception as error:
        log.exception("extract failed: platform=%s media=%s", platform.name, media_id)
        await report_error(status, error, edit=True)
        return

    try:
        if info.kind == "collection":  # playlist / album / set
            await batch.open_batch(message, status, [(platform, media_id, url, info, None)])
            return
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
    except Exception as error:
        log.exception("couldn't prepare media: platform=%s media=%s", platform.name, media_id)
        await report_error(status, error, edit=True)
