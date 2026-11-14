from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback, report_error
from app.config import get_settings
from app.platforms import registry
from app.services import captions, media
from app.services.delivery import retry_send
from app.services.jobs import request_download
from app.services.keyboards import formats_kb, languages_kb, parse_cb

log = logging.getLogger(__name__)
router = Router(name="callbacks")
ACTIONS = ("fmt", "dl", "back", "thumb", "desc", "music")


@router.callback_query(F.data.func(lambda d: bool(d) and d.split("|", 1)[0] in ACTIONS))
async def on_callback(cq: CallbackQuery, bot: Bot) -> None:
    action, platform_name, media_id, fmt, lang = parse_cb(cq.data)
    msg = cq.message
    if not isinstance(msg, Message):
        await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
        return
    await acknowledge_callback(cq)  # stop the spinner before Redis, extraction or queue publication
    try:
        registry.get(platform_name)
    except KeyError:
        await msg.answer(captions.EXPIRED)
        return
    try:
        info = await media.resolve_async(platform_name, media_id)
        if info is None:
            await msg.answer(captions.EXPIRED)
            return
        if action == "music":
            fmt, lang = "mp3", ""
        if action in ("fmt", "dl", "music") and not media.valid_choice(info, fmt, lang or None):
            await msg.answer(captions.UNAVAILABLE)
            return

        max_bytes = get_settings().max_file_bytes
        if action == "back":
            await msg.edit_reply_markup(reply_markup=formats_kb(info, max_bytes))
        elif action == "fmt" and fmt not in ("mp3", "all") and info.audio_tracks:
            await msg.edit_reply_markup(reply_markup=languages_kb(info, fmt))
        elif action in ("fmt", "dl", "music"):
            card = None if action == "music" else msg
            reply_to = (
                msg.message_id
                if action == "music"
                else (msg.reply_to_message.message_id if msg.reply_to_message else None)
            )
            error = await request_download(
                bot,
                info,
                fmt,
                lang or None,
                chat_id=msg.chat.id,
                user_id=cq.from_user.id,
                card=card,
                reply_to=reply_to,
            )
            if error:
                await msg.answer(error)
        elif action == "thumb" and info.thumbnail:
            async with media.thumbnail_file(info.thumbnail) as thumbnail:
                await retry_send(lambda: msg.answer_document(thumbnail, caption=escape(info.title[:200])))
        elif action == "desc":
            for chunk in captions.text_chunks(info.description or "No description."):
                await msg.answer(chunk, parse_mode=None)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            log.exception("callback rejected: %s %s", platform_name, media_id)
            await report_error(msg, error)
    except Exception as error:
        log.exception("callback failed: %s %s", platform_name, media_id)
        await report_error(msg, error)
