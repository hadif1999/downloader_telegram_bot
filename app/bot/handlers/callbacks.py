from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app.config import get_settings
from app.platforms import registry
from app.services import cache, captions
from app.services.delivery import send_media
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
    platform = registry.get(platform_name)

    info = cache.load_info(platform_name, media_id)
    if info is None and action != "music":
        await cq.answer(captions.EXPIRED, show_alert=True)
        return

    max_bytes = get_settings().max_file_bytes
    if action == "back":
        await msg.edit_reply_markup(reply_markup=formats_kb(info, max_bytes))
        await cq.answer()
    elif action == "fmt" and fmt != "mp3" and info.audio_tracks:
        await msg.edit_reply_markup(reply_markup=languages_kb(info, fmt))
        await cq.answer("Choose the audio language")
    elif action in ("fmt", "dl", "music"):
        await start_download(cq, bot, msg, platform, media_id, fmt, lang or None, action == "music")
    elif action == "thumb":
        await cq.answer()
        if info.thumbnail:
            await msg.answer_document(info.thumbnail, caption=escape(info.title))
    elif action == "desc":
        await cq.answer()
        text = escape(info.description or "No description.")
        for i in range(0, len(text), 4000):
            await msg.answer(text[i : i + 4000])


async def start_download(cq, bot, msg: Message, platform, media_id, fmt, lang, from_result: bool) -> None:
    chat_id, user_id = msg.chat.id, cq.from_user.id

    # instant re-send if this exact file was uploaded before
    if file_id := cache.get_file_id(platform.name, media_id, fmt, lang):
        await cq.answer()
        info = cache.load_info(platform.name, media_id)
        me = await bot.me()
        caption = captions.result_caption(info, me.username) if info else f"@{me.username}"
        await send_media(
            bot, chat_id, fmt, file_id, caption, platform.name, media_id, reply_to=msg.message_id
        )
        return

    info = cache.load_info(platform.name, media_id)
    fo = info.format(fmt) if info else None
    if fo and (fo.size or 0) > get_settings().max_file_bytes:
        await cq.answer(captions.TOO_BIG, show_alert=True)
        return
    if not cache.acquire_user_lock(user_id):
        await cq.answer(captions.BUSY, show_alert=True)
        return

    try:
        from app.worker.tasks import download_media

        if from_result or info is None:
            # tapped under a finished video: use a fresh status message
            status = await msg.reply(captions.QUEUED)
            status_id, is_caption, reply_to = status.message_id, False, msg.message_id
        else:
            await msg.edit_reply_markup(reply_markup=None)
            if msg.photo:
                await msg.edit_caption(caption=captions.status_caption(info, captions.QUEUED))
            else:
                await msg.edit_text(captions.status_caption(info, captions.QUEUED))
            status_id, is_caption = msg.message_id, bool(msg.photo)
            reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
        download_media.delay(
            chat_id, status_id, is_caption, platform.name, media_id, fmt, lang, user_id, reply_to
        )
        await cq.answer("Added to queue")
    except Exception:
        cache.release_user_lock(user_id)
        log.exception("enqueue failed")
        await cq.answer(captions.FAILED, show_alert=True)
