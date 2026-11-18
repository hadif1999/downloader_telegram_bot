"""Who uses the bot, and who may: activity statistics, admin bans and maintenance mode.

`ActivityMiddleware` runs right after the language middleware on every message, callback and
inline query or chosen inline result:
it records the user's activity in the background (throttled, see `stats.record_activity`), drops
updates from banned users and answers everyone but admins with a notice in maintenance mode.
The ban list and maintenance flag are cached for FLAGS_TTL seconds; admin changes refresh them
at once in this process.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChosenInlineResult, InlineQuery, Message

from app.bot.errors import acknowledge_callback
from app.config import get_settings
from app.i18n import t
from app.platforms import registry
from app.services import captions, stats
from app.services.delivery import edit_status

log = logging.getLogger(__name__)
router = Router(name="activity")
FLAGS_TTL = 15.0
DEFAULT_NOTICE = "default"  # stored instead of a custom notice: shown translated per user
PRESENT = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
}


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in get_settings().admin_ids


def notice_text(notice: str) -> str:
    if notice == DEFAULT_NOTICE:
        return t("🚧 The bot is under maintenance right now. Please try again a little later.")
    return notice


class Flags:
    """Cached ban list and maintenance notice (fail-open on Redis errors: keeps the last value)."""

    def __init__(self, ttl: float = FLAGS_TTL) -> None:
        self.ttl = ttl
        self.banned: set[int] = set()
        self.maintenance: str | None = None
        self.loaded = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self.loaded = 0.0

    async def get(self) -> Flags:
        if time.monotonic() - self.loaded < self.ttl:
            return self
        async with self._lock:
            if time.monotonic() - self.loaded >= self.ttl:
                try:
                    self.banned, self.maintenance = await asyncio.to_thread(self._read)
                except Exception:
                    log.warning("couldn't refresh the ban list / maintenance flag")
                self.loaded = time.monotonic()
        return self

    @staticmethod
    def _read() -> tuple[set[int], str | None]:
        return stats.banned_ids(), stats.maintenance()


flags = Flags()
_pending: set[asyncio.Task] = set()


def _record(event) -> None:
    user = event.from_user
    chat = (
        event.message.chat
        if isinstance(event, CallbackQuery) and event.message
        else getattr(event, "chat", None)
    )
    group = None
    if chat is not None and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        group = (chat.id, chat.type, chat.title or "")
    task = asyncio.create_task(
        asyncio.to_thread(
            stats.record_activity, user.id, user.full_name, user.username, user.language_code, group
        )
    )
    _pending.add(task)
    task.add_done_callback(_finished)


def _finished(task: asyncio.Task) -> None:
    _pending.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.debug("activity recording failed: %s", type(task.exception()).__name__)


def addressed(message: Message) -> bool:
    """Whether a maintenance notice should answer: private chats, or group links for the bot."""
    if message.chat.type == ChatType.PRIVATE:
        return True
    return registry.detect(message.text or message.caption or "") is not None


class ActivityMiddleware:
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)
        try:
            _record(event)
        except Exception:
            log.debug("couldn't schedule activity recording")
        if is_admin(user.id):
            return await handler(event, data)
        state = await flags.get()
        if user.id in state.banned:
            if isinstance(event, CallbackQuery):
                await acknowledge_callback(event)
            elif isinstance(event, InlineQuery):
                await _answer_inline(event, None)
            return None  # silently: a banned user learns nothing new
        if state.maintenance:
            text = notice_text(state.maintenance)
            if isinstance(event, CallbackQuery):
                await acknowledge_callback(event, text, show_alert=True)
            elif isinstance(event, InlineQuery):
                await _answer_inline(event, captions.INLINE_MAINTENANCE)
            elif isinstance(event, ChosenInlineResult) and event.inline_message_id:
                await edit_status(event.bot, 0, 0, text, False, inline_id=event.inline_message_id)
            elif isinstance(event, Message) and addressed(event):
                try:
                    await event.answer(text, request_timeout=5)
                except Exception:
                    log.debug("maintenance notice failed")
            return None
        return await handler(event, data)


async def _answer_inline(query: InlineQuery, notice: str | None) -> None:
    """No results; a notice (maintenance) shows as the button above the empty list."""
    from app.services.inline import results_button

    try:
        await query.answer(
            [],
            cache_time=30,
            is_personal=True,
            button=results_button(notice, "start") if notice else None,
            request_timeout=5,
        )
    except Exception:
        log.debug("inline notice failed")


@router.my_chat_member()
async def bot_membership_changed(update: ChatMemberUpdated) -> None:
    """Private chat: the user blocked/unblocked the bot. Groups: the bot was added/removed."""
    present = update.new_chat_member.status in PRESENT
    try:
        if update.chat.type == ChatType.PRIVATE:
            await asyncio.to_thread(stats.mark_blocked, update.chat.id, not present)
        else:
            await asyncio.to_thread(
                stats.record_chat_status, update.chat.id, update.chat.title or "", present
            )
    except Exception:
        log.warning("couldn't record a bot membership change")
