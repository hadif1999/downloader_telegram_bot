"""Require channel membership before any bot interaction can reach a download handler."""

from __future__ import annotations

import asyncio
import logging

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback
from app.bot.handlers.start import send_welcome
from app.config import Settings
from app.platforms import registry
from app.services import captions
from app.services.keyboards import force_join_kb

log = logging.getLogger(__name__)
CHECK_PREFIX = "force_join:"
MEMBER_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


class MembershipMiddleware(BaseMiddleware):
    def __init__(self, channel_id: int | None = None, title: str = "", url: str = "", bot_username: str = ""):
        self.channel_id, self.title, self.url, self.bot_username = channel_id, title, url, bot_username

    @classmethod
    async def configure(cls, bot: Bot, settings: Settings):
        if settings.force_join_channel_id is None:
            return cls()
        try:
            chat = await bot.get_chat(settings.force_join_channel_id, request_timeout=10)
            me = await asyncio.wait_for(bot.me(), timeout=10)
            admin = await bot.get_chat_member(chat.id, me.id, request_timeout=10)
        except (TelegramAPIError, TimeoutError):
            raise RuntimeError(
                "Cannot access FORCE_JOIN_CHANNEL_ID. Check the channel ID and add the bot as a channel administrator."
            ) from None
        if chat.type != ChatType.CHANNEL:
            raise RuntimeError("FORCE_JOIN_CHANNEL_ID must identify a Telegram channel.")
        if admin.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            raise RuntimeError(
                "The bot must be an administrator of FORCE_JOIN_CHANNEL_ID to verify membership reliably."
            )
        url = settings.force_join_channel_url or (
            f"https://t.me/{chat.username}" if chat.username else chat.invite_link
        )
        if not url:
            raise RuntimeError(
                "Set FORCE_JOIN_CHANNEL_URL to an existing invite link for this private channel."
            )
        return cls(chat.id, chat.title or "our channel", url, me.username or "")

    def relevant_message(self, message: Message) -> bool:
        if message.chat.type == ChatType.PRIVATE:
            return True
        text = (message.text or message.caption or "").strip()
        command = text.split(maxsplit=1)[0] if text else ""
        name, _, mention = command.partition("@")
        addressed = name in {"/start", "/help"} and (
            not mention or mention.casefold() == self.bot_username.casefold()
        )
        return addressed or registry.detect(text) is not None

    async def member(self, bot: Bot, user_id: int) -> bool:
        if self.channel_id is None:
            return True
        result = await bot.get_chat_member(self.channel_id, user_id, request_timeout=5)
        return result.status in MEMBER_STATUSES or (
            result.status == ChatMemberStatus.RESTRICTED and result.is_member
        )

    async def prompt(self, message: Message, user_id: int, notice: str, *, edit: bool = False):
        text = captions.force_join_caption(self.title, notice)
        markup = force_join_kb(self.url, user_id)
        if edit and message.text is not None:
            try:
                await message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
        else:
            await message.answer(text, reply_markup=markup)

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        check = callback and bool(event.data) and event.data.startswith(CHECK_PREFIX)
        if self.channel_id is None and not check:
            return await handler(event, data)
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        if isinstance(event, Message) and not self.relevant_message(event):
            return await handler(event, data)
        user = event.from_user
        if user is None or user.is_bot:
            return None  # anonymous/bot-authored messages cannot prove human membership
        message = event.message if callback else event
        if check:
            valid_button = (
                isinstance(message, Message)
                and message.reply_markup
                and any(
                    button.callback_data == event.data
                    for row in message.reply_markup.inline_keyboard
                    for button in row
                )
            )
            if event.data != f"{CHECK_PREFIX}{user.id}" or not valid_button:
                await acknowledge_callback(
                    event, "Send /start to check your own membership.", show_alert=True
                )
                return None
            await acknowledge_callback(event)  # don't leave a spinner while querying Telegram
        bot = data["bot"]
        try:
            joined = self.channel_id is None or await self.member(bot, user.id)
        except (TelegramAPIError, TimeoutError) as error:
            log.warning("membership verification unavailable: %s", type(error).__name__)
            if callback and not check:
                await acknowledge_callback(event, captions.JOIN_UNAVAILABLE, show_alert=True)
            if isinstance(message, Message):
                await self.prompt(message, user.id, captions.JOIN_UNAVAILABLE, edit=check)
            return None  # fail closed; an API outage never grants access
        if joined:
            if check:
                try:
                    await send_welcome(message, bot, edit=message.text is not None)
                except TelegramBadRequest as error:
                    if "message is not modified" not in error.message.lower():
                        raise
                return None
            return await handler(event, data)
        if callback and not check:
            await acknowledge_callback(event, captions.JOIN_REQUIRED, show_alert=True)
        if isinstance(message, Message):
            await self.prompt(
                message, user.id, captions.JOIN_NOT_FOUND if check else captions.JOIN_REQUIRED, edit=check
            )
        return None
