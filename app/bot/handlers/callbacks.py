from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback, report_error
from app.config import get_settings
from app.platforms import registry
from app.services import captions, inline, media, requests
from app.services.delivery import edit_status, retry_send
from app.services.jobs import request_download
from app.services.keyboards import CANCEL, SEP, formats_kb, inline_again_kb, languages_kb, parse_cb

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


@router.callback_query(F.data.startswith(CANCEL + SEP))
async def on_cancel(cq: CallbackQuery) -> None:
    """Cancel button on a queued or running download's status card."""
    request_id = cq.data.split(SEP, 1)[1]
    try:
        outcome = await asyncio.to_thread(requests.request_cancel, request_id, cq.from_user.id)
    except Exception:
        log.exception("cancel failed: request=%s", request_id)
        await acknowledge_callback(cq, captions.FAILED, show_alert=True)
        return
    if outcome == requests.CANCEL_UNKNOWN:
        await acknowledge_callback(cq, captions.CANCEL_DENIED, show_alert=True)
    elif outcome == requests.CANCEL_TOO_LATE:
        await acknowledge_callback(cq, captions.CANCEL_TOO_LATE, show_alert=True)
    elif outcome == requests.CANCEL_SIGNALLED:
        await acknowledge_callback(cq, captions.CANCEL_PENDING)  # the worker redraws the card
    else:
        await acknowledge_callback(cq, captions.CANCEL_DONE)
        await _show_cancelled(cq, request_id)


async def _show_cancelled(cq: CallbackQuery, request_id: str) -> None:
    """A queued job never reached a worker, so the bot redraws its card with the format menu."""
    msg = cq.message
    if not isinstance(msg, Message) and not cq.inline_message_id:
        return
    try:
        state = await asyncio.to_thread(requests.load, request_id, cq.from_user.id)
        platform_name, media_id, fmt, lang = (state.get("media", "").split("|") + [""] * 4)[:4]
        info = await media.resolve_async(platform_name, media_id) if media_id else None
        if not isinstance(msg, Message):  # an inline card in another chat: offer the formats again
            text = captions.INLINE_CANCELLED
            if info is not None:
                text = captions.status_caption(info, text, fmt, lang or None)
            await edit_status(
                cq.bot,
                0,
                0,
                text,
                False,
                inline_again_kb(inline.switch_query(info)),
                inline_id=cq.inline_message_id,
                preview=inline.preview_url(info),
            )
            return
        if info is None:
            text, kb = captions.CANCEL_DONE, None
        else:
            text = captions.status_caption(info, captions.CANCELLED, fmt, lang or None)
            kb = formats_kb(info, get_settings().max_file_bytes)
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
    except Exception:
        log.exception("couldn't redraw cancelled card: request=%s", request_id)
