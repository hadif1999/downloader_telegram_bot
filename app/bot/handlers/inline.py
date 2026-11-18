"""Inline mode: `@bot <link>` in any chat lists the formats; choosing one downloads it there.

The query handler answers within Telegram's few seconds: it reuses the metadata cache and the
bot's extraction slots, and when a lookup runs late it answers "still looking" while the lookup
finishes in the background (the next keystroke then answers from the cache). Choosing a result
(or its Start button, when Telegram's chosen-result update doesn't arrive) queues an ordinary
request whose status card is the inline message (see `jobs.start_download(inline_id=...)`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChosenInlineResult, InlineQuery

from app.bot.errors import acknowledge_callback
from app.config import get_settings
from app.i18n import t
from app.models import MediaInfo
from app.platforms import registry
from app.services import cache, captions, inline, media, stats
from app.services.delivery import DEFINITE, edit_status, put_inline
from app.services.errors import user_message
from app.services.jobs import request_download
from app.services.keyboards import INLINE, SEP, inline_again_kb, inline_result_kb, inline_start_kb

log = logging.getLogger(__name__)
router = Router(name="inline")

# Lookups in flight in this process, shared by the queries a user sends while typing.
_lookups: dict[tuple[str, str], asyncio.Task] = {}


def _forget(key: tuple[str, str], task: asyncio.Task) -> None:
    if _lookups.get(key) is task:
        del _lookups[key]
    if not task.cancelled() and task.exception() is not None:
        log.info("inline lookup failed: platform=%s error=%s", key[0], type(task.exception()).__name__)


async def lookup(platform_name: str, media_id: str, url: str, timeout: float) -> MediaInfo | None:
    """The link's metadata, or None while the lookup is still running after `timeout` seconds."""
    key = (platform_name, media_id)
    task = _lookups.get(key)
    if task is None:
        task = asyncio.create_task(media.resolve_async(platform_name, media_id, url))
        _lookups[key] = task
        task.add_done_callback(lambda done: _forget(key, done))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return None


async def answer(query: InlineQuery, results=(), *, button=None, cache_time: int = 0) -> None:
    try:
        await query.answer(
            list(results), cache_time=cache_time, is_personal=True, button=button, request_timeout=10
        )
    except TelegramAPIError as error:
        # "query is too old" after a slow lookup: the user's next keystroke asks again.
        log.info("inline answer rejected: %s", type(error).__name__)


@router.inline_query()
async def on_inline_query(query: InlineQuery, bot: Bot) -> None:
    found = registry.detect(query.query.strip())
    if found is None:
        await answer(
            query, button=inline.results_button(t("📖 Paste a link here to download it")), cache_time=300
        )
        return
    platform, media_id, url = found
    try:
        info = await lookup(platform.name, media_id, url, get_settings().inline_lookup_timeout)
    except Exception as error:
        await answer(query, button=inline.results_button(user_message(error)))
        return
    if info is None:
        await answer(query, button=inline.results_button(t("⏳ Still looking… add a space to refresh")))
        return
    formats = [option.key for option in inline.offered(info, get_settings().max_file_bytes)]
    cached = {
        fmt: await asyncio.to_thread(cache.get_file_id, info.platform, info.id, fmt, None) for fmt in formats
    }
    await asyncio.to_thread(inline.save_choices, query.from_user.id, info, formats)
    me = await bot.me()
    results = inline.build_results(query.from_user.id, info, cached, me.username)
    button = await asyncio.to_thread(inline.more_button, info, bool(results))
    # Short cache: a format downloaded a minute ago should come back as an instant result.
    await answer(query, results, button=button, cache_time=5)


@router.chosen_inline_result()
async def on_chosen(chosen: ChosenInlineResult, bot: Bot) -> None:
    if not chosen.inline_message_id:
        # Only cached results come without a keyboard: Telegram has already sent the file.
        choice = await asyncio.to_thread(inline.load_choice, chosen.result_id)
        if choice:
            await asyncio.to_thread(stats.record_download, choice[0], "cached", chosen.from_user.id)
        return
    await begin(bot, chosen.from_user.id, chosen.result_id, chosen.inline_message_id)


@router.callback_query(F.data.startswith(INLINE + SEP))
async def on_start_button(cq: CallbackQuery, bot: Bot) -> None:
    if not cq.inline_message_id:
        await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
        return
    alert = await begin(bot, cq.from_user.id, cq.data.split(SEP, 1)[1], cq.inline_message_id)
    await acknowledge_callback(cq, alert, show_alert=bool(alert))


async def begin(bot: Bot, user_id: int, rid: str, inline_id: str) -> str | None:
    """Start the download of a chosen result once. Returns a notice for a Start button press."""
    choice = await asyncio.to_thread(inline.load_choice, rid)
    if choice is None:
        await edit_status(bot, 0, 0, captions.EXPIRED, False, inline_id=inline_id)
        return captions.EXPIRED
    platform_name, media_id, fmt, owner = choice
    if owner != user_id:
        return captions.INLINE_NOT_YOURS
    if not await asyncio.to_thread(inline.claim_start, inline_id):
        return captions.INLINE_STARTED
    info = None
    try:
        registry.get(platform_name)
        info = await media.resolve_async(platform_name, media_id)
        if info is None or not media.valid_choice(info, fmt, None):
            raise LookupError
        if await _send_cached(bot, info, fmt, user_id, inline_id):
            return None
        error = await request_download(
            bot, info, fmt, None, chat_id=user_id, user_id=user_id, inline_id=inline_id
        )
    except (KeyError, LookupError):
        error = captions.EXPIRED if info is None else captions.UNAVAILABLE
    except Exception as failure:
        log.exception("inline start failed: platform=%s media=%s", platform_name, media_id)
        error = user_message(failure)
    if error:
        # Refused before queueing (busy, too large, expired): let the Start button try again.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(inline.release_start, inline_id)
        text = captions.status_caption(info, error, fmt) if info else error
        markup = (
            inline_start_kb(rid) if error == captions.BUSY else inline_again_kb(inline.switch_query(info))
        )
        await edit_status(
            bot, 0, 0, text, False, markup, inline_id=inline_id, preview=inline.preview_url(info)
        )
    return None


async def _send_cached(bot: Bot, info: MediaInfo, fmt: str, user_id: int, inline_id: str) -> bool:
    """A format cached since the results were listed: put it on the card without a download."""
    cached = await asyncio.to_thread(cache.get_file_id, info.platform, info.id, fmt, None)
    ready = inline.cached_media(fmt, cached)
    if ready is None:
        return False
    kind, file_id = ready
    me = await bot.me()
    caption = captions.result_caption(info, me.username)
    try:
        await put_inline(
            bot, inline_id, kind, file_id, caption, inline_result_kb(inline.switch_query(info), kind)
        )
    except DEFINITE:
        return False  # e.g. an invalid file_id: download it again
    except TelegramAPIError:
        log.warning("cached inline edit unconfirmed: platform=%s media=%s", info.platform, info.id)
        return True  # never queue a second copy behind an edit that may have worked
    await asyncio.to_thread(stats.record_download, info.platform, "cached", user_id)
    return True


async def check_storage(bot: Bot) -> None:
    """At startup: INLINE_STORAGE_CHAT_ID must be a chat the bot can post files to."""
    configured = get_settings().inline_storage_chat
    if configured is None:
        return
    name = f"INLINE_STORAGE_CHAT_ID {configured}"
    try:
        me = await asyncio.wait_for(bot.me(), timeout=10)
        chat = await bot.get_chat(configured, request_timeout=10)
        member = await bot.get_chat_member(chat.id, me.id, request_timeout=10)
    except (TelegramAPIError, TimeoutError):
        raise RuntimeError(f"Cannot access {name}. Check the ID and add the bot to that chat.") from None
    if chat.type == ChatType.CHANNEL:
        allowed = member.status == ChatMemberStatus.CREATOR or (
            member.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(member, "can_post_messages", False))
        )
    else:
        allowed = member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        }
    if not allowed:
        raise RuntimeError(f"The bot must be able to post in {name} (a channel admin with 'Post messages').")
