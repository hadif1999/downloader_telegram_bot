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

from app.config import get_settings
from app.platforms import registry
from app.platforms.common import download_file
from app.platforms.ytdlp import TooLarge, UserError
from app.services import cache, captions, media, requests
from app.services.delivery import CheckpointError, Journal, edit_status, local_file, send_gallery, send_media
from app.services.telegram import make_bot
from app.worker.celery_app import celery

log = logging.getLogger(__name__)
PROGRESS_EVERY_SEC = 3
PROGRESS_MIN_STEP = 5


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


@celery.task(bind=True, name="download_media")
def download_media(self, chat_id, msg_id, is_caption, platform, media_id, fmt, lang, user_id, reply_to=None):
    try:
        return asyncio.run(
            _run(
                chat_id, msg_id, is_caption, platform, media_id, fmt, lang, reply_to, user_id, self.request.id
            )
        )
    except requests.LeaseBusy as error:
        # Redelivery can race a live original. Wait for its completion or lease expiry.
        raise self.retry(exc=error, countdown=30, max_retries=get_settings().queue_ttl // 30 + 1) from error


async def _run(
    chat_id, msg_id, is_caption, platform_name, media_id, fmt, lang, reply_to, user_id, request_id
):
    async with requests.execution(request_id, user_id) as owner:
        bot = make_bot()
        s = get_settings()
        dest = s.download_dir / uuid.uuid4().hex
        info = None
        journal = None

        async def status(text):
            body = captions.status_caption(info, text, fmt, lang) if info else text
            await edit_status(bot, chat_id, msg_id, body, is_caption)

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
            unit = "files" if info.kind == "gallery" else "bytes"
            await status(captions.progress_text(0, None))
            loop = asyncio.get_running_loop()
            last = {"t": 0.0, "pct": -100}

            def on_progress(done, total):
                pct = int(done * 100 / total) if total else 0
                now = time.monotonic()
                if now - last["t"] < PROGRESS_EVERY_SEC or (total and pct - last["pct"] < PROGRESS_MIN_STEP):
                    return
                last.update(t=now, pct=pct)
                future = asyncio.run_coroutine_threadsafe(
                    status(captions.progress_text(done, total, unit=unit)), loop
                )
                try:
                    future.result(timeout=8)
                except Exception:
                    future.cancel()  # late progress must not overwrite UPLOADING

            dest.mkdir(parents=True, exist_ok=True)
            result = await asyncio.to_thread(platform.download, info, fmt, lang, dest, on_progress)
            paths = [p for p, _ in result.files] or [result.path]
            if any(Path(p).stat().st_size > s.max_file_bytes for p in paths):
                raise TooLarge(media_id)
            await status(captions.UPLOADING)
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
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id, msg_id)
        except requests.DeliveryUncertain:
            await status(captions.UNCERTAIN)
            raise
        except Exception as error:
            if journal:
                with contextlib.suppress(Exception):
                    await journal.phase("failed")
            text = (
                captions.TOO_BIG
                if isinstance(error, TooLarge)
                else f"⚠️ {error}"
                if isinstance(error, UserError)
                else captions.FAILED
            )
            await status(text)
            raise  # monitoring must see unexpected download/delivery errors as failures
        finally:
            shutil.rmtree(dest, ignore_errors=True)
            await bot.session.close()
