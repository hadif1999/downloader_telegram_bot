"""Admin broadcast: copy one message to many users, throttled and stoppable.

One broadcast runs at a time, inside the bot process (a restart stops it; the report then shows
how far it got). Messages are copied (no "forwarded from" header) at RATE per second, below
Telegram's ~30/s bulk limit. A RetryAfter is waited out and the same user retried; users who
blocked the bot are recorded so later broadcasts and statistics skip them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app.services import stats

log = logging.getLogger(__name__)
RATE = 20  # messages per second
REPORT_EVERY = 5.0  # seconds between progress card edits
MAX_RETRY_AFTER = 120


@dataclass
class Broadcast:
    source_chat: int
    source_message: int
    recipients: list[int]
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    stopped: bool = False
    finished: bool = False
    started: float = field(default_factory=time.monotonic)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def total(self) -> int:
        return len(self.recipients)

    def stop(self) -> None:
        self.stop_event.set()


current: Broadcast | None = None
_task: asyncio.Task | None = None


def running() -> bool:
    return current is not None and not current.finished


async def deliver(bot: Bot, b: Broadcast, user_id: int) -> None:
    for _ in range(3):
        try:
            await bot.copy_message(user_id, b.source_chat, b.source_message, request_timeout=15)
            b.sent += 1
            return
        except TelegramRetryAfter as error:
            await asyncio.sleep(min(error.retry_after, MAX_RETRY_AFTER) + 1)
        except TelegramForbiddenError:
            b.blocked += 1
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stats.mark_blocked, user_id)
            return
        except (TelegramBadRequest, TelegramAPIError, TimeoutError):
            b.failed += 1
            return
    b.failed += 1


async def run(bot: Bot, b: Broadcast, report) -> None:
    """Send to every recipient, calling `report(b)` every REPORT_EVERY seconds and at the end."""
    last = time.monotonic()
    try:
        for user_id in b.recipients:
            if b.stop_event.is_set():
                b.stopped = True
                break
            began = time.monotonic()
            await deliver(bot, b, user_id)
            if time.monotonic() - last >= REPORT_EVERY:
                last = time.monotonic()
                with contextlib.suppress(Exception):
                    await report(b)
            await asyncio.sleep(max(0.0, 1 / RATE - (time.monotonic() - began)))
    except asyncio.CancelledError:
        b.stopped = True
        raise
    finally:
        b.finished = True
        log.info("broadcast finished: sent=%s blocked=%s failed=%s", b.sent, b.blocked, b.failed)
        with contextlib.suppress(Exception):
            await report(b)


def start(bot: Bot, b: Broadcast, report) -> bool:
    """Start `b` in the background unless another broadcast is running."""
    global current, _task
    if running():
        return False
    current = b
    _task = asyncio.create_task(run(bot, b, report), name="admin-broadcast")
    return True
