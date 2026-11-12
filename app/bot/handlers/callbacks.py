from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app.config import get_settings
from app.services import captions, media
from app.services.jobs import request_download
from app.services.keyboards import formats_kb, languages_kb, parse_cb

log = logging.getLogger(__name__)
router = Router(name="callbacks")

ACTIONS = ("fmt", "dl", "back", "thumb", "desc", "music")


@router.callback_query(F.data.func(lambda d: d.split("|", 1)[0] in ACTIONS))
async def on_callback(cq: CallbackQuery, bot: Bot) -> None:
    action, platform_name, media_id, fmt, lang = parse_cb(cq.data)
    msg = cq.message
    if not isinstance(msg, Message):
        await cq.answer(captions.EXPIRED, show_alert=True)
        return
    try:
        info = await asyncio.to_thread(media.resolve, platform_name, media_id)
    except Exception:
        log.exception("re-extract failed: %s %s", platform_name, media_id)
        info = None
    if info is None:
        await cq.answer(captions.EXPIRED, show_alert=True)
        return

    max_bytes = get_settings().max_file_bytes
    if action == "back":
        await msg.edit_reply_markup(reply_markup=formats_kb(info, max_bytes))
        await cq.answer()
    elif action == "fmt" and fmt not in ("mp3", "all") and info.audio_tracks:
        await msg.edit_reply_markup(reply_markup=languages_kb(info, fmt))
        await cq.answer("Choose the audio language")
    elif action in ("fmt", "dl", "music"):
        # "music" is tapped under a finished video: keep that message, report in a new one
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
        await cq.answer(error or "Added to queue", show_alert=bool(error))
    elif action == "thumb":
        await cq.answer()
        if info.thumbnail:
            await msg.answer_document(info.thumbnail, caption=escape(info.title))
    elif action == "desc":
        await cq.answer()
        text = escape(info.description or "No description.")
        for i in range(0, len(text), 4000):
            await msg.answer(text[i : i + 4000])
