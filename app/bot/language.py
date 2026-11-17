"""Per-user interface language: the middleware that applies it and the /language picker."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, Message

from app import i18n
from app.bot.errors import acknowledge_callback
from app.services import captions
from app.services.keyboards import LANGUAGE, SEP, language_kb

log = logging.getLogger(__name__)
router = Router(name="language")


class LanguageMiddleware:
    """Runs before every other middleware so membership prompts and errors are translated too."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        lang = None
        if user is not None:
            try:
                lang = await asyncio.to_thread(i18n.load, user.id)
            except Exception:
                log.warning("couldn't load the user's language; using the default")
        data["ui_language"] = i18n.use(lang)
        return await handler(event, data)


@router.message(Command("language"))
async def choose_language(message: Message) -> None:
    current = i18n.current()
    await message.answer(captions.language_menu(current), reply_markup=language_kb(current))


@router.callback_query(F.data.startswith(LANGUAGE + SEP))
async def set_language(cq: CallbackQuery) -> None:
    lang = cq.data.split(SEP, 1)[1]
    if lang not in i18n.LANGUAGES:
        await acknowledge_callback(cq)
        return
    try:
        await asyncio.to_thread(i18n.save, cq.from_user.id, lang)
    except Exception:
        log.exception("couldn't save the user's language")
        await acknowledge_callback(cq, captions.FAILED, show_alert=True)
        return
    i18n.use(lang)  # the confirmation is already in the new language
    await acknowledge_callback(cq, i18n.label(lang))
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(captions.language_changed(), reply_markup=None)
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise


COMMANDS = {
    "start": "Start and see what I can download",
    "language": "Change the bot's language 🌐",
    "help": "How to use the bot",
}


async def publish_commands(bot) -> None:
    """The command menu, in every language (Telegram picks by the client's language)."""
    for lang in i18n.LANGUAGES:
        i18n.use(lang)
        commands = [BotCommand(command=name, description=i18n.t(text)) for name, text in COMMANDS.items()]
        try:
            await bot.set_my_commands(commands, language_code=lang, request_timeout=10)
            if lang == i18n.default():
                await bot.set_my_commands(commands, request_timeout=10)  # clients in other languages
        except Exception:
            log.warning("couldn't publish the %s command menu", lang)
