"""The hidden /admin panel: monitoring and management for the users listed in ADMIN_IDS.

Every handler of this router is behind `IsAdmin`. For anyone else /admin is ordinary text, the
command is not in the bot's menu, and the panel's buttons are ignored. The panel is one message
edited in place; text input (user lookup, direct message, broadcast, maintenance notice) uses
short FSM states that a button or a new /admin cancels.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import i18n
from app.bot import activity
from app.bot.admin import broadcast, views
from app.bot.errors import acknowledge_callback
from app.i18n import t
from app.services import captions, clock, delivery, ops, requests, stats

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return activity.is_admin(user.id if user else None)


router.message.filter(IsAdmin(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(IsAdmin())


class Input(StatesGroup):
    find = State()
    message = State()
    broadcast = State()
    notice = State()


# ------------------------------------------------------------------ plumbing


async def show(target: Message, text: str, markup, *, edit: bool = True) -> Message | None:
    """Edit the panel in place (or send it anew when it can't be edited)."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return target
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return target
            log.debug("admin panel edit failed, sending anew: %s", error.message)
    return await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def _edit_only(target: Message, text: str, markup) -> None:
    with contextlib.suppress(TelegramBadRequest):  # not modified, or the panel was deleted
        await target.edit_text(text, reply_markup=markup, disable_web_page_preview=True)


_answered: set[str] = set()  # callbacks a handler already answered with a toast


async def notify(cq: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Answer the button press once, with an optional toast (or alert)."""
    if cq.id in _answered:
        return
    _answered.add(cq.id)
    await acknowledge_callback(cq, text, show_alert=alert)


@router.message(Command("admin"))
async def open_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = views.home(await asyncio.to_thread(views.collect_home))
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ------------------------------------------------------------------ button actions


async def home(cq, arg, state, bot):
    await state.clear()
    return views.home(await asyncio.to_thread(views.collect_home))


async def users(cq, arg, state, bot):
    return views.users(await asyncio.to_thread(views.collect_users))


async def downloads(cq, arg, state, bot):
    return views.downloads(await asyncio.to_thread(views.collect_downloads))


async def queue(cq, arg, state, bot):
    return views.queue(await asyncio.to_thread(views.collect_queue))


async def system(cq, arg, state, bot):
    return views.system(await asyncio.to_thread(views.collect_system))


async def top(cq, arg, state, bot):
    return views.top(await asyncio.to_thread(stats.top_users, 10))


async def errors(cq, arg, state, bot):
    return views.errors(await asyncio.to_thread(stats.recent_errors, 12), time.time())


def _find_target(request_id: str) -> dict | None:
    """A running (live) or queued download by request ID, with what is needed to cancel it."""
    for item in stats.live_downloads():
        if item.get("id") == request_id:
            return {**item, "queued": False}
    for task in ops.queued(200):
        if task.id == request_id and task.name == "download_media":
            return {
                "id": task.id,
                "platform": task.platform,
                "fmt": task.fmt,
                "user": task.user,
                "chat": task.chat,
                "message": task.message,
                "is_caption": task.is_caption,
                "inline": task.inline,
                "queued": True,
            }
    return None


async def kill(cq, arg, state, bot):
    target = await asyncio.to_thread(_find_target, arg)
    if target is None:
        await notify(cq, t("That download has already finished."), alert=True)
        return await queue(cq, arg, state, bot)
    return views.confirm_cancel(target)


async def kill_confirmed(cq, arg, state, bot):
    target = await asyncio.to_thread(_find_target, arg)
    if target is None or not target.get("user"):
        await notify(cq, t("That download has already finished."), alert=True)
        return await queue(cq, arg, state, bot)
    outcome = await asyncio.to_thread(ops.cancel, arg, int(target["user"]))
    if (
        outcome == requests.CANCEL_STOPPED
        and target.get("queued")
        and (target.get("message") or target.get("inline"))
    ):
        # The worker will skip it; nobody else redraws the user's "queued" card. In their language.
        admin_language = i18n.current()
        try:
            i18n.use(await asyncio.to_thread(i18n.load, int(target["user"])))
            inline_id = target.get("inline")
            await delivery.edit_status(
                bot,
                target["chat"],
                target["message"],
                captions.INLINE_CANCELLED if inline_id else captions.CANCELLED,
                target["is_caption"],
                inline_id=inline_id,
            )
        except Exception:
            log.debug("couldn't update the cancelled request's card")
        finally:
            i18n.use(admin_language)
    replies = {
        requests.CANCEL_STOPPED: t("✅ Cancelled before it started."),
        requests.CANCEL_SIGNALLED: t("✅ Stopping: the worker cancels it within a few seconds."),
        requests.CANCEL_TOO_LATE: t("It is already uploading and can't be stopped."),
    }
    await notify(cq, replies.get(outcome, t("That download has already finished.")), alert=True)
    log.info("admin %s cancelled request %s: outcome=%s", cq.from_user.id, arg, outcome)
    return await queue(cq, arg, state, bot)


async def find(cq, arg, state, bot):
    await state.set_state(Input.find)
    return views.prompt(t("🔎 Send a user's numeric ID or @username (of someone who has used the bot)."))


async def user(cq, arg, state, bot):
    await state.clear()
    return await _user_card(int(arg))


async def _user_card(user_id: int):
    profile = await asyncio.to_thread(stats.profile, user_id)
    lives = await asyncio.to_thread(stats.live_downloads)
    running = next((item for item in lives if item.get("user") == user_id), None)
    return views.user_card(profile, time.time(), running)


async def ban(cq, arg, state, bot):
    user_id = int(arg)
    if activity.is_admin(user_id):
        await notify(cq, t("Admins can't be banned."), alert=True)
        return await _user_card(user_id)
    await asyncio.to_thread(stats.set_banned, user_id, True)
    activity.flags.invalidate()
    log.info("admin %s banned user %s", cq.from_user.id, user_id)
    await notify(cq, t("⛔ Banned: the bot now ignores this user."))
    return await _user_card(user_id)


async def unban(cq, arg, state, bot):
    user_id = int(arg)
    await asyncio.to_thread(stats.set_banned, user_id, False)
    activity.flags.invalidate()
    log.info("admin %s unbanned user %s", cq.from_user.id, user_id)
    await notify(cq, t("✅ Unbanned."))
    return await _user_card(user_id)


async def message_user(cq, arg, state, bot):
    await state.set_state(Input.message)
    await state.update_data(target=int(arg))
    return views.prompt(
        t("✉️ Send the message for user <code>{id}</code>. It is copied to them as is.", id=int(arg))
    )


async def broadcast_menu(cq, arg, state, bot):
    await state.clear()
    if broadcast.running():
        return views.broadcast_menu({}, True)

    def counts():
        now = time.time()
        return {
            name: stats.count_recipients(None if seconds is None else now - seconds)
            for name, seconds in views.AUDIENCES.items()
        }

    return views.broadcast_menu(await asyncio.to_thread(counts), False)


async def broadcast_audience(cq, arg, state, bot):
    if arg not in views.AUDIENCES:
        return await broadcast_menu(cq, arg, state, bot)
    await state.set_state(Input.broadcast)
    await state.update_data(audience=arg)
    return views.prompt(
        t(
            "📣 Send the message to broadcast: text, photo, video or any other message. You'll see it before it goes out."
        )
    )


async def broadcast_go(cq, arg, state, bot):
    data = await state.get_data()
    await state.clear()
    if "source" not in data:
        return await broadcast_menu(cq, arg, state, bot)
    if broadcast.running():
        return views.broadcast_menu({}, True)
    seconds = views.AUDIENCES.get(data["audience"])
    since = None if seconds is None else time.time() - seconds
    recipients = await asyncio.to_thread(lambda: list(stats.recipients(since)))
    job = broadcast.Broadcast(cq.message.chat.id, data["source"], recipients)
    panel = cq.message

    async def report(b):
        # Interim reports only edit (a deleted panel must not turn into a message every 5 s);
        # the final report is sent anew if the panel is gone.
        text, markup = views.broadcast_progress(b)
        if b.finished:
            await show(panel, text, markup)
        else:
            await _edit_only(panel, text, markup)

    broadcast.start(bot, job, report)
    log.info("admin %s started a broadcast to %s users", cq.from_user.id, job.total)
    return views.broadcast_progress(job)


async def broadcast_stop(cq, arg, state, bot):
    if broadcast.current is not None and broadcast.running():
        broadcast.current.stop()
        await notify(cq, t("⏹ Stopping after the current message…"))
        return views.broadcast_progress(broadcast.current)
    return await broadcast_menu(cq, arg, state, bot)


async def maintenance(cq, arg, state, bot):
    await state.clear()
    notice = await asyncio.to_thread(stats.maintenance)
    return views.maintenance(activity.notice_text(notice) if notice else None)


async def _set_maintenance(notice, admin_id):
    await asyncio.to_thread(stats.set_maintenance, notice)
    activity.flags.invalidate()
    log.info("admin %s turned maintenance %s", admin_id, "on" if notice else "off")
    return views.maintenance(activity.notice_text(notice) if notice else None)


async def maintenance_on(cq, arg, state, bot):
    return await _set_maintenance(activity.DEFAULT_NOTICE, cq.from_user.id)


async def maintenance_off(cq, arg, state, bot):
    return await _set_maintenance(None, cq.from_user.id)


async def maintenance_custom(cq, arg, state, bot):
    await state.set_state(Input.notice)
    return views.prompt(t("✏️ Send the notice users should see during maintenance."))


async def export(cq, arg, state, bot):
    def build() -> tuple[bytes, int]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        rows = -1
        for row in stats.export_users():
            writer.writerow(row)
            rows += 1
        return buffer.getvalue().encode("utf-8-sig"), rows  # BOM: Excel reads the names correctly

    await notify(cq, t("📄 Preparing the export…"))
    payload, rows = await asyncio.to_thread(build)
    name = f"users-{clock.local().strftime('%Y%m%d-%H%M')}.csv"
    await cq.message.answer_document(
        BufferedInputFile(payload, name), caption=t("📄 {count} users", count=views.num(rows))
    )
    return None


async def close(cq, arg, state, bot):
    await state.clear()
    try:
        await cq.message.delete()
    except TelegramBadRequest:
        await cq.message.edit_reply_markup(reply_markup=None)
    return None


ACTIONS = {
    "home": home,
    "users": users,
    "dl": downloads,
    "queue": queue,
    "sys": system,
    "top": top,
    "err": errors,
    "kill": kill,
    "killok": kill_confirmed,
    "find": find,
    "u": user,
    "ban": ban,
    "unban": unban,
    "msg": message_user,
    "bc": broadcast_menu,
    "bca": broadcast_audience,
    "bcgo": broadcast_go,
    "bcstop": broadcast_stop,
    "mnt": maintenance,
    "mnton": maintenance_on,
    "mntoff": maintenance_off,
    "mntown": maintenance_custom,
    "csv": export,
    "close": close,
}


@router.callback_query(F.data.startswith(views.PREFIX + "|"))
async def on_button(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, action, arg = (cq.data.split("|", 2) + ["", ""])[:3]
    handler = ACTIONS.get(action)
    if handler is None or not isinstance(cq.message, Message):
        await acknowledge_callback(cq)
        return
    try:
        try:
            result = await handler(cq, arg, state, bot)
        except Exception:
            log.exception("admin action failed: %s", action)
            await notify(cq, t("⚠️ That didn't work; the details are in the bot's log."), alert=True)
            return
        await notify(cq)
        if result is not None:
            await show(cq.message, *result)
    finally:
        _answered.discard(cq.id)


# ------------------------------------------------------------------ text input


@router.message(StateFilter(Input.find))
async def find_input(message: Message, state: FSMContext) -> None:
    user_id = await asyncio.to_thread(stats.find_user, message.text or "")
    if user_id is None:
        text, markup = views.prompt(
            t("🔎 No user found. Send a numeric ID or the @username of someone who has used the bot.")
        )
        await message.answer(text, reply_markup=markup)
        return
    await state.clear()
    text, markup = await _user_card(user_id)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.message(StateFilter(Input.message))
async def message_input(message: Message, state: FSMContext, bot: Bot) -> None:
    target = (await state.get_data()).get("target")
    await state.clear()
    try:
        await bot.copy_message(target, message.chat.id, message.message_id, request_timeout=15)
        reply = t("✅ Sent to <code>{id}</code>.", id=target)
    except TelegramForbiddenError:
        await asyncio.to_thread(stats.mark_blocked, target)
        reply = t("🚫 Not sent: the user blocked the bot.")
    except TelegramBadRequest as error:
        reply = t("❌ Not sent: {reason}", reason=escape(error.message[:200]))
    log.info("admin %s messaged user %s", message.from_user.id, target)
    text, markup = await _user_card(target)
    await message.answer(reply)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.message(StateFilter(Input.broadcast))
async def broadcast_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    audience = data.get("audience", "all")
    seconds = views.AUDIENCES.get(audience)
    since = None if seconds is None else time.time() - seconds
    count = await asyncio.to_thread(stats.count_recipients, since)
    await state.update_data(source=message.message_id)
    labels = {
        "all": t("everyone"),
        "30d": t("active in 30 days"),
        "7d": t("active in 7 days"),
        "24h": t("active in 24 hours"),
    }
    await message.copy_to(message.chat.id)  # the exact preview of what users will receive
    text, markup = views.broadcast_confirm(labels.get(audience, audience), count)
    await message.answer(text, reply_markup=markup)


@router.message(StateFilter(Input.notice))
async def notice_input(message: Message, state: FSMContext) -> None:
    notice = (message.text or "").strip()
    if not notice:
        await message.answer(t("✏️ Send the notice as text."))
        return
    await state.clear()
    text, markup = await _set_maintenance(notice, message.from_user.id)
    await message.answer(text, reply_markup=markup)
