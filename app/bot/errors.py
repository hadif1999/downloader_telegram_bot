"""Last-resort update handling and best-effort error feedback."""

import logging
from html import unescape

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.services import captions
from app.services.errors import user_message

log = logging.getLogger(__name__)


async def acknowledge_callback(callback: CallbackQuery, text: str | None = None, *, show_alert=False):
    try:
        if text is None:
            await callback.answer(request_timeout=5)
        else:
            await callback.answer(
                captions._short(unescape(text), 180), show_alert=show_alert, request_timeout=5
            )
    except (TelegramAPIError, TimeoutError) as error:
        # Old callbacks and network failures must not prevent the actual operation.
        log.warning("callback acknowledgement failed: %s", type(error).__name__)


async def report_error(message: Message, error: Exception, *, edit=False) -> None:
    text = user_message(error)
    try:
        if edit:
            try:
                await message.edit_text(text, request_timeout=5)
                return
            except TelegramBadRequest as rejected:
                reason = rejected.message.lower()
                if "message is not modified" in reason:
                    return
                if not any(
                    part in reason for part in ("message to edit not found", "message can't be edited")
                ):
                    raise
        await message.answer(text, request_timeout=5)
    except (TelegramAPIError, TimeoutError) as failure:
        # Reporting a failure must never trigger another report or an ambiguous send retry.
        log.warning("error feedback unavailable: %s", type(failure).__name__)
    except Exception:
        log.exception("unexpected error feedback failure")


async def on_error(event: ErrorEvent) -> bool:
    update = event.update
    log.error(
        "unhandled bot update: update_id=%s error=%s",
        update.update_id,
        type(event.exception).__name__,
        exc_info=(type(event.exception), event.exception, event.exception.__traceback__),
    )
    callback = update.callback_query
    try:
        if callback:
            await acknowledge_callback(callback, user_message(event.exception), show_alert=True)
        else:
            message = update.message or update.edited_message
            if isinstance(message, Message):
                await report_error(message, event.exception)
    except Exception:
        log.exception("last-resort error feedback failed: update_id=%s", update.update_id)
    return True
