from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from aiogram.types import FSInputFile

from app.config import get_settings
from app.platforms import registry
from app.platforms.youtube import TooLarge
from app.services import cache, captions
from app.services.delivery import edit_status, local_file, send_media, sent_file_id
from app.services.telegram import make_bot
from app.worker.celery_app import celery

log = logging.getLogger(__name__)

PROGRESS_EVERY_SEC = 3
PROGRESS_MIN_STEP = 5  # percent


def make_thumb(url: str | None, dest: Path) -> Path | None:
    """Telegram wants a JPEG thumbnail <=320px and <200KB."""
    if not url:
        return None
    out = dest / "thumb.jpg"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", url, "-vf", "scale=320:-2", "-q:v", "5", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=60)
    except Exception:
        log.warning("thumbnail failed for %s", url)
        return None
    return out if out.exists() else None


@celery.task(name="download_media")
def download_media(
    chat_id: int,
    msg_id: int,
    is_caption: bool,
    platform: str,
    media_id: str,
    fmt: str,
    lang: str | None,
    user_id: int,
    reply_to: int | None = None,
) -> None:
    try:
        asyncio.run(_run(chat_id, msg_id, is_caption, platform, media_id, fmt, lang, reply_to))
    finally:
        cache.release_user_lock(user_id)


async def _run(chat_id, msg_id, is_caption, platform_name, media_id, fmt, lang, reply_to) -> None:
    s = get_settings()
    platform = registry.get(platform_name)
    bot = make_bot()
    dest = s.download_dir / uuid.uuid4().hex
    info = None
    try:
        info = cache.load_info(platform_name, media_id)
        if info is None:
            info = await asyncio.to_thread(platform.extract, platform.url_for(media_id))
            cache.save_info(info)

        async def status(text: str) -> None:
            body = captions.status_caption(info, text) if is_caption else text
            await edit_status(bot, chat_id, msg_id, body, is_caption)

        await status(captions.progress_text(0, None))

        loop = asyncio.get_running_loop()
        last = {"t": 0.0, "pct": -100}

        def on_progress(done: int, total: int | None) -> None:  # called from yt-dlp's thread
            pct = int(done * 100 / total) if total else 0
            now = time.monotonic()
            if now - last["t"] >= PROGRESS_EVERY_SEC and (
                pct - last["pct"] >= PROGRESS_MIN_STEP or not total
            ):
                last.update(t=now, pct=pct)
                asyncio.run_coroutine_threadsafe(status(captions.progress_text(done, total)), loop)

        dest.mkdir(parents=True, exist_ok=True)
        result = await asyncio.to_thread(platform.download, info, fmt, lang, dest, on_progress)
        if Path(result.path).stat().st_size > s.max_file_bytes:
            raise TooLarge(media_id)

        await status(captions.UPLOADING)
        thumb = await asyncio.to_thread(make_thumb, info.thumbnail, dest)
        extra = {"duration": result.duration}
        if thumb:
            extra["thumbnail"] = FSInputFile(thumb)  # API only accepts uploads here
        if fmt == "mp3":
            extra |= {"title": result.title, "performer": result.performer}
        else:
            extra |= {"width": result.width, "height": result.height}

        me = await bot.get_me()
        msg = await send_media(
            bot,
            chat_id,
            fmt,
            local_file(result.path),
            captions.result_caption(info, me.username),
            platform_name,
            media_id,
            reply_to=reply_to,
            **extra,
        )
        if file_id := sent_file_id(msg):
            cache.save_file_id(platform_name, media_id, fmt, lang, file_id)
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, msg_id)
    except Exception as e:
        if not isinstance(e, TooLarge):
            log.exception("download failed: %s %s %s", platform_name, media_id, fmt)
        text = captions.TOO_BIG if isinstance(e, TooLarge) else captions.FAILED
        if is_caption and info is not None:
            text = captions.status_caption(info, text)
        await edit_status(bot, chat_id, msg_id, text, is_caption)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        await bot.session.close()
