from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from aiogram.types import FSInputFile

from app import i18n
from app.config import get_settings
from app.platforms import registry
from app.platforms.common import describe, download_file, retry_delay
from app.platforms.ytdlp import TooLarge, UserError, retryable
from app.services import batches, cache, captions, jobs, keyboards, media, progress, requests, stats, storage
from app.services.delivery import CheckpointError, Journal, edit_status, local_file, send_gallery, send_media
from app.services.errors import user_message
from app.services.telegram import make_bot
from app.worker.celery_app import celery

log = logging.getLogger(__name__)
PROGRESS_EVERY_SEC = 3  # status card refresh in private chats
GROUP_PROGRESS_EVERY_SEC = 5  # groups allow ~20 edits per minute


def make_thumb(url: str | None, dest: Path) -> Path | None:
    if not url:
        return None
    out = dest / "thumb.jpg"
    try:
        # ffmpeg only sees a bounded local file, never an untrusted network URL.
        source = download_file(url, dest / "thumb-source", timeout=15, max_bytes=5 * 1024 * 1024)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease",
                "-q:v",
                "5",
                str(out),
            ],
            check=True,
            timeout=30,
        )
    except Exception:
        log.warning("thumbnail conversion failed")
        return None
    return out if out.exists() and out.stat().st_size <= 200_000 else None


def download_estimate(info, fmt: str) -> int:
    """Approximate bytes a download needs on disk; MP3 sources are larger than the output."""
    option = info.format(fmt)
    size = option.size if option and option.size else 0
    return size * 2 if fmt == "mp3" else size


@celery.task(bind=True, name="download_media")
def download_media(
    self,
    chat_id,
    msg_id,
    is_caption,
    platform,
    media_id,
    fmt,
    lang,
    user_id,
    reply_to=None,
    ui_language=None,
    batch=None,
):
    i18n.use(ui_language)  # the requester's language for every status edit of this task
    retrying, failure = False, None
    try:
        return asyncio.run(
            _run(
                chat_id, msg_id, is_caption, platform, media_id, fmt, lang, reply_to, user_id, self.request.id
            )
        )
    except requests.LeaseBusy as error:
        # Redelivery can race a live original. Wait for its completion or lease expiry.
        retrying = True
        raise self.retry(exc=error, countdown=30, max_retries=get_settings().queue_ttl // 30 + 1) from error
    except Exception as error:
        failure = error
        raise
    finally:
        if batch and not retrying:
            # The user lease is released by now: record the item and start the next one.
            finish_batch_item(batch[0], int(batch[1]), self.request.id, user_id, failure)


OUTCOMES = {"done": batches.SENT, "cancelled": batches.SKIPPED, "uncertain": batches.UNCERTAIN}


def finish_batch_item(batch_id, index, request_id, user_id, failure=None) -> None:
    try:
        phase = requests.load(request_id, user_id).get("phase")
        code = OUTCOMES.get(phase or "", batches.FAILED)
        reason = ""
        if code == batches.FAILED:
            reason = user_message(failure) if failure else captions.FAILED
        elif code == batches.UNCERTAIN:
            reason = captions.UNCERTAIN
        batches.record(batch_id, index, code, reason)
    except Exception:
        log.exception("couldn't record batch item: batch=%s index=%s", batch_id, index)
    try:
        advance_batch.apply_async((batch_id, index), retry=False)
    except Exception:
        log.exception("couldn't continue batch: batch=%s after=%s", batch_id, index)


@celery.task(bind=True, name="advance_batch")
def advance_batch(self, batch_id, after):
    """Start the next selected item of a batch (and keep going past items served from cache or
    failing right away). The queued item's download_media calls this again when it ends."""
    try:
        return asyncio.run(_advance(batch_id, int(after)))
    except batches.Busy as error:
        # The user started another download in between: try the same item again shortly.
        raise self.retry(exc=error, countdown=20, max_retries=get_settings().queue_ttl // 20 + 1) from error


async def _advance(batch_id: str, after: int) -> None:
    batch = await asyncio.to_thread(batches.load, batch_id)
    if batch is None:
        return
    i18n.use(batch.lang)
    bot = make_bot(for_upload=True)

    async def draw(current: int | None = None, final: bool = False):
        fresh = await asyncio.to_thread(batches.load, batch_id) or batch
        if final:
            text, markup = captions.batch_done(fresh), None
        else:
            text, markup = captions.batch_progress(fresh, current), keyboards.batch_running_kb(fresh)
        await edit_status(bot, fresh.chat, fresh.card, text, False, markup)

    try:
        while True:
            index = await asyncio.to_thread(batches.begin, batch_id, after)
            if index == batches.FINISHED:
                await draw(final=True)
                return
            if index < 0:
                return  # another advance owns this step, or the batch is gone
            item = batch.items[index]
            await draw(index)
            queued = False
            try:
                info = await asyncio.to_thread(media.resolve, item.platform, item.media_id, item.url)
                if info is None:
                    raise UserError(captions.EXPIRED)
                fmt = media.preset_choice(info, batch.quality)
                if fmt is None:
                    error = captions.TOO_BIG
                else:
                    error, queued = await jobs.start_download(
                        bot,
                        info,
                        fmt,
                        None,
                        chat_id=batch.chat,
                        user_id=batch.user,
                        reply_to=batch.reply_to,
                        batch=(batch_id, index),
                    )
            except Exception as failure:
                log.warning(
                    "batch item failed before queueing: batch=%s index=%s error=%s",
                    batch_id,
                    index,
                    type(failure).__name__,
                )
                error = user_message(failure)
            if queued:
                return  # its download_media continues the batch
            if error == captions.BUSY:
                await asyncio.to_thread(batches.release, batch_id, index, after)
                raise batches.Busy
            if error is None:
                code = batches.CACHED
            elif error == captions.UNCERTAIN:
                code = batches.UNCERTAIN
            elif error == captions.TOO_BIG:
                code = batches.SKIPPED
            else:
                code = batches.FAILED
            await asyncio.to_thread(batches.record, batch_id, index, code, error or "")
            after = index
    finally:
        try:
            await bot.session.close()
        except Exception:
            log.exception("worker bot session cleanup failed: batch=%s", batch_id)


async def _run(
    chat_id, msg_id, is_caption, platform_name, media_id, fmt, lang, reply_to, user_id, request_id
):
    bot = make_bot(for_upload=True)
    s = get_settings()
    dest = s.download_dir / uuid.uuid4().hex
    info = None
    journal = None
    slot = None
    # Statistics: what became of this request (None = nothing to count, e.g. a replay).
    outcome, error_text, delivered = None, "", 0
    started, wall_started = time.monotonic(), time.time()

    monitor = progress.Monitor(timeout=s.download_timeout)
    ticker = None
    edit_lock = asyncio.Lock()
    shown = {"text": None}

    async def status(text, markup=None):
        body = captions.status_caption(info, text, fmt, lang) if info else text
        await edit_status(bot, chat_id, msg_id, body, is_caption, markup)

    async def show():
        """Render the live card; edits are serialized so a slow edit can't land out of order."""
        async with edit_lock:
            text = monitor.render()
            markup = keyboards.cancel_kb(request_id) if monitor.cancellable else None
            if (text, markup is None) != shown["text"]:
                shown["text"] = (text, markup is None)
                await status(text, markup)

    def publish_live():
        """The admin's live queue view: what this download is doing right now."""
        if info is not None:
            snapshot = {
                "platform": platform_name,
                "fmt": fmt,
                "lang": lang,
                "user": user_id,
                "chat": chat_id,
                "title": (info.title or "")[:80],
                "started": wall_started,
                **monitor.snapshot(),
            }
            stats.publish_live(request_id, snapshot)

    async def watch():
        every = GROUP_PROGRESS_EVERY_SEC if chat_id < 0 else PROGRESS_EVERY_SEC
        while True:
            await asyncio.sleep(every)
            with contextlib.suppress(progress.Cancelled):
                await check_cancel()  # sets the flag; the download thread raises on its next report
            await show()
            await asyncio.to_thread(publish_live)

    async def stop_ticker():
        if ticker is not None and not ticker.done():
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def check_cancel():
        if not monitor.cancel.is_set():
            try:
                if await asyncio.to_thread(requests.cancel_requested, request_id, user_id):
                    monitor.cancel.set()
            except Exception:
                log.debug("cancel flag check failed: request=%s", request_id)
        if monitor.cancel.is_set():
            raise progress.Cancelled

    async def download_with_retries(platform, info):
        """Re-run a failed download up to DOWNLOAD_RETRIES times in the same directory, so
        finished parts, finished streams and partial .part files are reused, not fetched again."""
        for attempt in range(s.download_retries + 1):
            try:
                return await asyncio.to_thread(platform.download, info, fmt, lang, dest, monitor)
            except Exception as error:
                if attempt == s.download_retries or not retryable(error):
                    raise
                wait = retry_delay(attempt + 1)
                log.warning(
                    "download attempt %s failed, retrying in %ss: request=%s error=%s",
                    attempt + 1,
                    wait,
                    request_id,
                    type(error).__name__,
                )
                monitor.restarting(attempt + 1, s.download_retries, describe(error), wait)
                await show()
                await asyncio.sleep(wait)
                await check_cancel()

    try:
        async with requests.execution(request_id, user_id) as owner:
            try:
                if owner is None:
                    state = await asyncio.to_thread(requests.load, request_id, user_id)
                    if not state:
                        await status(captions.EXPIRED)
                    elif state.get("phase") == "uncertain":
                        await status(captions.UNCERTAIN)
                    return
                journal = await Journal.load(request_id, user_id, owner)
                if journal.total and len(journal.sent) == journal.total:
                    with contextlib.suppress(Exception):
                        await journal.phase("done")
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id, msg_id)
                    return

                platform = registry.get(platform_name)
                info = await asyncio.to_thread(media.resolve, platform_name, media_id)
                if info is None:
                    raise UserError(captions.EXPIRED)
                if not media.valid_choice(info, fmt, lang):
                    raise UserError(captions.UNAVAILABLE)
                monitor.unit = "files" if info.kind == "gallery" else "bytes"
                await show()
                await asyncio.to_thread(publish_live)
                ticker = asyncio.create_task(watch())

                dest.mkdir(parents=True, exist_ok=True)
                # A pinned slot keeps retained files from being evicted while they upload.
                slot = await asyncio.to_thread(
                    storage.open_slot, s.download_dir, platform_name, media_id, fmt, lang
                )
                result = slot.result
                if result is not None:
                    log.info("serving retained media: request=%s", request_id)
                else:
                    await asyncio.to_thread(
                        storage.ensure_space, s.download_dir, download_estimate(info, fmt)
                    )
                    result = await download_with_retries(platform, info)
                paths = [p for p, _ in result.files] or [result.path]
                if any(Path(p).stat().st_size > s.max_file_bytes for p in paths):
                    raise TooLarge(media_id)
                if slot.result is None:
                    result = await asyncio.to_thread(slot.store, result)
                await check_cancel()  # last chance: nothing is sent after this point
                delivered = sum(Path(p).stat().st_size for p in paths)
                monitor.set_stage("upload", size=delivered)
                await show()
                me = await bot.get_me()
                caption = captions.result_caption(info, me.username)
                try:
                    if result.files:
                        await send_gallery(
                            bot,
                            chat_id,
                            [(local_file(p), t) for p, t in result.files],
                            caption,
                            reply_to,
                            journal=journal,
                        )
                    else:
                        thumb = await asyncio.to_thread(make_thumb, info.thumbnail, dest)
                        extra = {"duration": result.duration}
                        if thumb:
                            extra["thumbnail"] = FSInputFile(thumb)
                        if fmt == "mp3":
                            extra |= {"title": result.title, "performer": result.performer}
                        else:
                            extra |= {"width": result.width, "height": result.height}
                        await send_media(
                            bot,
                            chat_id,
                            fmt,
                            local_file(result.path),
                            caption,
                            platform_name,
                            media_id,
                            reply_to=reply_to,
                            music_button=info.kind == "video",
                            journal=journal,
                            **extra,
                        )
                except CheckpointError:
                    if len(journal.sent) != journal.total:
                        raise requests.DeliveryUncertain from None
                    log.exception("delivery accepted but checkpoint failed: %s", request_id)

                # Caching is an optimization, never turn an accepted upload into failure.
                try:
                    file_ids = json.dumps(journal.sent) if result.files else journal.sent[0][1]
                    await asyncio.to_thread(cache.save_file_id, platform_name, media_id, fmt, lang, file_ids)
                except Exception:
                    log.exception("couldn't cache delivered media: %s", request_id)
                with contextlib.suppress(Exception):
                    await journal.phase("done")
                outcome = "ok"
                await stop_ticker()
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id, msg_id)
            except requests.DeliveryUncertain:
                raise
            except progress.Cancelled:
                await stop_ticker()
                log.info("download cancelled by user: request=%s", request_id)
                outcome = "cancel"
                with contextlib.suppress(Exception):
                    await journal.phase("cancelled")
                await status(captions.CANCELLED, keyboards.formats_kb(info, s.max_file_bytes))
                return
            except Exception:
                if journal:
                    with contextlib.suppress(Exception):
                        await journal.phase("failed")
                raise  # monitoring must see unexpected download/delivery errors as failures
            finally:
                await stop_ticker()  # before any final status edit, so a tick can't overwrite it
    except requests.LeaseBusy:
        raise  # the task wrapper retries competing redeliveries without altering their status
    except Exception as error:
        outcome = "uncertain" if isinstance(error, requests.DeliveryUncertain) else "fail"
        error_text = f"{type(error).__name__}: {user_message(error)}"
        await status(user_message(error))
        raise
    finally:
        await asyncio.to_thread(stats.clear_live, request_id)
        if outcome is not None:
            await asyncio.to_thread(
                stats.record_download,
                platform_name,
                outcome,
                user_id,
                delivered if outcome == "ok" else 0,
                time.monotonic() - started,
                error_text,
            )
        shutil.rmtree(dest, ignore_errors=True)
        if slot is not None:
            # Releases the pin, then enforces the retention limits and sweeps orphans.
            await asyncio.to_thread(slot.close)
        try:
            await bot.session.close()
        except Exception:
            log.exception("worker bot session cleanup failed: request=%s", request_id)
