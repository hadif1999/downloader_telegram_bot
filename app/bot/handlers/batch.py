"""Playlists, albums, sets and messages with several links: one summary card, then the items are
downloaded and sent one by one (see app.services.batches and the worker's advance_batch)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app import i18n
from app.bot.errors import acknowledge_callback
from app.config import get_settings
from app.i18n import t
from app.models import MediaInfo
from app.platforms import registry
from app.services import batches, captions, media
from app.services.delivery import edit_status
from app.services.errors import user_message
from app.services.keyboards import (
    BATCH,
    SEP,
    batch_pages,
    batch_picker_kb,
    batch_running_kb,
    batch_summary_kb,
)

log = logging.getLogger(__name__)
router = Router(name="batch")
LOOKUP_EVERY_SEC = 3

# (platform, media_id, url, info or None, error text or None)
Lookup = tuple[object, str, str, MediaInfo | None, str | None]


def _link_label(platform, url: str) -> str:
    short = url.split("://", 1)[-1].removeprefix("www.")
    return f"{platform.emoji} {captions._short(short, 40)}"


async def lookup_many(message: Message, status: Message, found, skipped: int) -> None:
    """Look up several links at once (bounded by the shared extraction slots), then open a batch."""
    done = 0

    async def show():
        await edit_status(
            status.bot,
            status.chat.id,
            status.message_id,
            t("🔎 Reading {n} links… {done} of {n} done", n=len(found), done=done),
            False,
        )

    async def one(platform, media_id, url) -> Lookup:
        nonlocal done
        try:
            async with media.extraction_slots:
                info = await asyncio.to_thread(media.resolve, platform.name, media_id, url)
            return platform, media_id, url, info, None if info else captions.EXPIRED
        except Exception as error:
            log.warning("batch lookup failed: platform=%s error=%s", platform.name, type(error).__name__)
            return platform, media_id, url, None, user_message(error)
        finally:
            done += 1

    async def ticker():
        while True:
            await asyncio.sleep(LOOKUP_EVERY_SEC)
            await show()

    await show()
    progress = asyncio.create_task(ticker())
    try:
        results = await asyncio.gather(*(one(*link) for link in found))
    finally:
        progress.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress
    await open_batch(message, status, list(results), skipped)


def _items(results: list[Lookup]) -> tuple[list[batches.Item], list[list[str]], int]:
    """Flatten looked-up links (collections into their entries) -> (items, failed links, cut)."""
    limit = get_settings().collection_max_items
    items: list[batches.Item] = []
    failed: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    cut = 0

    def add(item: batches.Item) -> None:
        nonlocal cut
        if (item.platform, item.media_id) in seen:
            return
        if len(items) >= limit:
            cut += 1
            return
        seen.add((item.platform, item.media_id))
        items.append(item)

    for platform, _media_id, url, info, error in results:
        if info is None:
            failed.append([_link_label(platform, url), error or captions.FAILED])
            continue
        if info.kind != "collection":
            add(
                batches.Item(
                    info.platform, info.id, info.url, info.title, info.duration, info.kind, info.uploader
                )
            )
            continue
        audio = bool(info.extra.get("audio"))
        for entry in info.entries:
            hit = registry.match(entry.url)
            if hit is None:
                continue  # the entry's platform is disabled
            entry_platform, entry_id = hit
            kind = "audio" if audio else "video"
            add(
                batches.Item(
                    entry_platform.name,
                    entry_id,
                    entry.url,
                    entry.title,
                    entry.duration,
                    kind,
                    entry.uploader,
                )
            )
    return items, failed, cut


async def open_batch(message: Message, status: Message, results: list[Lookup], skipped: int = 0) -> None:
    """Store a draft batch and turn `status` into its summary card."""
    items, failed, cut = _items(results)
    if not items:
        reasons = "\n".join(f"• {label}: {captions._plain(reason, 120)}" for label, reason in failed[:10])
        text = t("⚠️ Nothing to download in this message.")
        await status.edit_text(text + (f"\n\n{reasons}" if reasons else ""))
        return
    single = results[0][3] if len(results) == 1 else None
    audio_only = all(item.kind == "audio" for item in items)
    if single is not None and single.kind == "collection":
        platform = results[0][0]
        meta = {
            "kind": "collection",
            "title": single.title,
            "uploader": single.uploader,
            "source": platform.title,
            "emoji": platform.emoji,
            "audio": bool(single.extra.get("audio")),
            "total": max(int(single.extra.get("total") or 0), len(single.entries)),
        }
    else:
        meta = {"kind": "links", "links": len(results), "total": len(items) + cut}
    meta |= {"failed": failed, "skipped": skipped, "audio_only": audio_only}
    batch_id = await asyncio.to_thread(
        batches.create,
        user=message.from_user.id,
        chat=message.chat.id,
        card=status.message_id,
        reply_to=message.message_id,
        lang=i18n.current(),
        items=items,
        meta=meta,
        quality="mp3" if audio_only else batches.DEFAULT_PRESET,
    )
    batch = await asyncio.to_thread(batches.load, batch_id)
    await _show(status, captions.batch_summary(batch), batch_summary_kb(batch))


async def _show(msg: Message, text: str, markup) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise


async def _refused(cq: CallbackQuery, code: int) -> bool:
    """Alert for a refused batch operation. Returns True when the caller must stop."""
    if code == batches.EXPIRED:
        await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
    elif code == batches.NOT_OWNER:
        await acknowledge_callback(
            cq, t("Only the person who sent these links can use these buttons."), show_alert=True
        )
    elif code == batches.WRONG_PHASE:
        await acknowledge_callback(cq, t("This list has already started downloading."), show_alert=True)
    elif code == batches.EMPTY:
        await acknowledge_callback(cq, t("Select at least one item first."), show_alert=True)
    else:
        return False
    return True


def _check(batch: batches.Batch | None, user: int) -> int:
    if batch is None:
        return batches.EXPIRED
    if batch.user != user:
        return batches.NOT_OWNER
    if batch.phase != "draft":
        return batches.WRONG_PHASE
    return batches.OK


@router.callback_query(F.data.startswith(BATCH + SEP))
async def on_batch(cq: CallbackQuery, bot: Bot) -> None:
    _, batch_id, op, arg = (cq.data.split(SEP) + ["", "", "", ""])[:4]
    msg = cq.message
    if not isinstance(msg, Message):
        await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
        return
    user = cq.from_user.id
    try:
        if op in ("q", "t", "all", "none"):
            if (op == "q" and arg not in batches.PRESETS) or (op == "t" and not arg.isdigit()):
                await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
                return
            if await _refused(cq, await asyncio.to_thread(batches.edit, batch_id, user, op, arg)):
                return
            await acknowledge_callback(cq)
            batch = await asyncio.to_thread(batches.load, batch_id)
            if batch is None:
                return
            if op == "q":
                await _show(msg, captions.batch_summary(batch), batch_summary_kb(batch))
            else:
                page = int(arg) // batches.PICKER_PAGE if op == "t" else int(arg or 0)
                await _picker(msg, batch, page)
        elif op in ("p", "back"):
            batch = await asyncio.to_thread(batches.load, batch_id)
            if await _refused(cq, _check(batch, user)):
                return
            await acknowledge_callback(cq)
            if op == "back":
                await _show(msg, captions.batch_summary(batch), batch_summary_kb(batch))
            else:
                await _picker(msg, batch, int(arg) if arg.isdigit() else 0)
        elif op == "go":
            if await _refused(cq, await asyncio.to_thread(batches.start, batch_id, user)):
                return
            await acknowledge_callback(cq)
            await _start(msg, batch_id)
        elif op in ("x", "stop"):
            outcome = await asyncio.to_thread(batches.stop, batch_id, user)
            if await _refused(cq, outcome):
                return
            batch = await asyncio.to_thread(batches.load, batch_id)
            if outcome == 2:
                await acknowledge_callback(cq)
                await _show(msg, t("🚫 Cancelled. Nothing was downloaded."), None)
            else:
                await acknowledge_callback(cq, t("⏹ Stopping after the current item…"))
                if batch is not None:
                    await _show(msg, captions.batch_progress(batch), None)
        else:
            await acknowledge_callback(cq, captions.EXPIRED, show_alert=True)
    except Exception:
        log.exception("batch callback failed: batch=%s op=%s", batch_id, op)
        with contextlib.suppress(Exception):
            await msg.answer(captions.FAILED)


async def _picker(msg: Message, batch: batches.Batch, page: int) -> None:
    pages = batch_pages(batch)
    page = min(max(page, 0), pages - 1)
    await _show(msg, captions.batch_picker(batch, page, pages), batch_picker_kb(batch, page))


async def _start(msg: Message, batch_id: str) -> None:
    from app.worker.tasks import advance_batch

    batch = await asyncio.to_thread(batches.load, batch_id)
    if batch is None:
        return
    await _show(msg, captions.batch_progress(batch), batch_running_kb(batch))
    try:
        await asyncio.to_thread(advance_batch.apply_async, (batch_id, -1), retry=False)
    except Exception as error:
        log.exception("couldn't queue batch: %s", batch_id)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(batches.stop, batch_id, batch.user)
        await _show(msg, user_message(error), None)
