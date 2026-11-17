"""Require membership of every configured channel before any interaction reaches a handler."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.bot.errors import acknowledge_callback
from app.bot.handlers.start import send_welcome
from app.config import Settings
from app.i18n import t
from app.platforms import registry
from app.services import captions
from app.services.keyboards import LANGUAGE, SEP, force_join_kb

log = logging.getLogger(__name__)
CHECK_PREFIX = "force_join:"
MEMBER_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


@dataclass(frozen=True)
class Channel:
    id: int
    title: str
    url: str


class MembershipMiddleware(BaseMiddleware):
    def __init__(self, channels: list[Channel] | tuple[Channel, ...] = (), bot_username: str = ""):
        self.channels, self.bot_username = tuple(channels), bot_username

    @classmethod
    async def configure(cls, bot: Bot, settings: Settings):
        if not settings.force_join_channel_id:
            return cls()
        try:
            me = await asyncio.wait_for(bot.me(), timeout=10)
        except (TelegramAPIError, TimeoutError):
            raise RuntimeError("Cannot reach Telegram to verify FORCE_JOIN_CHANNEL_ID.") from None
        channels: list[Channel] = []
        for index, configured in enumerate(settings.force_join_channel_id):
            channel = await cls._resolve(bot, me.id, configured, settings.force_join_url(index))
            if any(existing.id == channel.id for existing in channels):
                raise RuntimeError(f"FORCE_JOIN_CHANNEL_ID lists channel {configured} more than once.")
            channels.append(channel)
        return cls(channels, me.username or "")

    @staticmethod
    async def _resolve(bot: Bot, bot_id: int, configured: int | str, url: str) -> Channel:
        """Validate one configured channel at startup: a channel the bot administers, with a link."""
        name = f"FORCE_JOIN_CHANNEL_ID entry {configured}"
        try:
            chat = await bot.get_chat(configured, request_timeout=10)
            admin = await bot.get_chat_member(chat.id, bot_id, request_timeout=10)
        except (TelegramAPIError, TimeoutError):
            raise RuntimeError(
                f"Cannot access {name}. Check the channel ID and add the bot as a channel administrator."
            ) from None
        if chat.type != ChatType.CHANNEL:
            raise RuntimeError(f"{name} must identify a Telegram channel.")
        if admin.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            raise RuntimeError(f"The bot must be an administrator of {name} to verify membership reliably.")
        url = url or (f"https://t.me/{chat.username}" if chat.username else chat.invite_link)
        if not url:
            raise RuntimeError(
                f"Set the matching FORCE_JOIN_CHANNEL_URL entry to an existing invite link for the "
                f"private channel {configured}."
            )
        return Channel(chat.id, chat.title or "our channel", url)

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

    @staticmethod
    async def _joined(bot: Bot, channel: Channel, user_id: int) -> bool:
        result = await bot.get_chat_member(channel.id, user_id, request_timeout=5)
        return result.status in MEMBER_STATUSES or (
            result.status == ChatMemberStatus.RESTRICTED and result.is_member
        )

    async def missing(self, bot: Bot, user_id: int) -> list[Channel]:
        """Channels the user has not joined, checked concurrently. Any lookup failure raises."""
        if not self.channels:
            return []
        joined = await asyncio.gather(*(self._joined(bot, channel, user_id) for channel in self.channels))
        return [channel for channel, ok in zip(self.channels, joined, strict=True) if not ok]

    @staticmethod
    def _notice(single: str, many: str, channels) -> str:
        return single if len(channels) <= 1 else many

    async def prompt(
        self,
        message: Message,
        user_id: int,
        notice: str,
        *,
        edit: bool = False,
        channels: list[Channel] | tuple[Channel, ...] | None = None,
    ):
        channels = channels or self.channels
        text = captions.force_join_caption([channel.title for channel in channels], notice)
        markup = force_join_kb([(channel.title, channel.url) for channel in channels], user_id)
        if edit and message.text is not None:
            try:
                await message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
        else:
            await message.answer(text, reply_markup=markup)

    @staticmethod
    def language_request(event) -> bool:
        """/language and its buttons work before joining, so the join prompt can be read."""
        if isinstance(event, CallbackQuery):
            return bool(event.data) and event.data.startswith(LANGUAGE + SEP)
        text = (event.text or "").strip() if isinstance(event, Message) else ""
        return bool(text) and text.split(maxsplit=1)[0].partition("@")[0] == "/language"

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        if self.language_request(event):
            return await handler(event, data)
        check = callback and bool(event.data) and event.data.startswith(CHECK_PREFIX)
        if not self.channels and not check:
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
                    event, t("Send /start to check your own membership."), show_alert=True
                )
                return None
            await acknowledge_callback(event)  # don't leave a spinner while querying Telegram
        bot = data["bot"]
        try:
            missing = await self.missing(bot, user.id)
        except (TelegramAPIError, TimeoutError) as error:
            log.warning("membership verification unavailable: %s", type(error).__name__)
            if callback and not check:
                await acknowledge_callback(event, captions.JOIN_UNAVAILABLE, show_alert=True)
            if isinstance(message, Message):
                await self.prompt(message, user.id, captions.JOIN_UNAVAILABLE, edit=check)
            return None  # fail closed; an API outage never grants access
        if not missing:
            if check:
                try:
                    await send_welcome(message, bot, edit=message.text is not None)
                except TelegramBadRequest as error:
                    if "message is not modified" not in error.message.lower():
                        raise
                return None
            return await handler(event, data)
        required = self._notice(captions.JOIN_REQUIRED, captions.JOIN_REQUIRED_MANY, missing)
        if callback and not check:
            await acknowledge_callback(event, required, show_alert=True)
        if isinstance(message, Message):
            not_found = self._notice(captions.JOIN_NOT_FOUND, captions.JOIN_NOT_FOUND_MANY, missing)
            await self.prompt(
                message, user.id, not_found if check else required, edit=check, channels=missing
            )
        return None
