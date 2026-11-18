"""Translate failures at user-facing boundaries; keep raw diagnostics in logs."""

import errno
import subprocess
from html import escape
from urllib.error import HTTPError, URLError

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiohttp import ClientError
from billiard.exceptions import SoftTimeLimitExceeded
from redis.exceptions import RedisError
from yt_dlp.utils import DownloadError

from app.i18n import t
from app.platforms.common import TooLargeError
from app.platforms.ytdlp import UserError
from app.services import captions, requests

TEXTS = {
    "TEMPORARY": "⚠️ The service is temporarily unavailable. Please try again later.",
    "TIMED_OUT": "⚠️ This request took too long. Please try again later or choose a smaller format.",
    "SOURCE_UNAVAILABLE": "⚠️ Couldn't access this media. It may be private, removed, or require login.",
    "RATE_LIMITED": "⚠️ Too many requests. Please wait a little before trying again.",
    "NO_ACCESS": "⚠️ I can't send media here. Check the bot's chat permissions and try again.",
}


def __getattr__(name: str) -> str:
    """TEMPORARY, TIMED_OUT, ...: the text in the current language (see app.i18n)."""
    if name in TEXTS:
        return t(TEXTS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def user_message(error: Exception) -> str:
    seen = set()
    while isinstance(error, Exception) and id(error) not in seen:
        seen.add(id(error))
        if text := _known_message(error):
            return text
        error = error.__cause__
    return captions.FAILED


def _known_message(error: Exception) -> str | None:
    # Check uncertainty before inspecting causes: a timeout after upload is not a safe retry.
    if isinstance(error, requests.DeliveryUncertain):
        return captions.UNCERTAIN
    if isinstance(error, (TooLargeError, TelegramEntityTooLarge)):
        return captions.TOO_BIG
    if isinstance(error, UserError):
        return f"⚠️ {escape(captions._short(t(str(error)), 500))}"
    if isinstance(error, TelegramForbiddenError):
        return t(TEXTS["NO_ACCESS"])
    if isinstance(error, TelegramMigrateToChat):
        return t("⚠️ This group has moved. Please send your link again in the new group.")
    if isinstance(error, TelegramRetryAfter):
        return t(TEXTS["RATE_LIMITED"])
    if isinstance(error, TelegramBadRequest):
        if "too big" in error.message.lower() or "too large" in error.message.lower():
            return captions.TOO_BIG
        return captions.FAILED
    if isinstance(error, HTTPError):
        if error.code == 429:
            return t(TEXTS["RATE_LIMITED"])
        return t(TEXTS["SOURCE_UNAVAILABLE"]) if error.code in {401, 403, 404, 410} else t(TEXTS["TEMPORARY"])
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired, SoftTimeLimitExceeded)):
        return t(TEXTS["TIMED_OUT"])
    if isinstance(error, subprocess.CalledProcessError):
        return t("⚠️ Couldn't process this media. Please try another format.")
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EMFILE, errno.ENFILE}:
        return t(TEXTS["TEMPORARY"])
    if isinstance(
        error, (RedisError, ConnectionError, URLError, ClientError, TelegramNetworkError, TelegramServerError)
    ):
        return t(TEXTS["TEMPORARY"])
    if isinstance(error, DownloadError):
        return t(TEXTS["SOURCE_UNAVAILABLE"])
    # DeliveryNotSent and other wrappers retain a typed cause. Unknown programming errors
    # never expose their message, paths, URLs or credentials to a Telegram user.
    return None
