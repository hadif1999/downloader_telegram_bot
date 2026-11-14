import asyncio
import errno
import io
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from urllib.error import HTTPError

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import Update
from redis.exceptions import ConnectionError as RedisConnectionError

from app.bot import errors
from app.logging_config import redact, redact_logs
from app.platforms.ytdlp import TooLarge, UserError
from app.services import cache, captions, delivery
from app.services.errors import (
    NO_ACCESS,
    RATE_LIMITED,
    SOURCE_UNAVAILABLE,
    TEMPORARY,
    TIMED_OUT,
    user_message,
)


@pytest.mark.parametrize(
    "error,expected",
    [
        (TooLarge("private URL"), captions.TOO_BIG),
        (TimeoutError("internal details"), TIMED_OUT),
        (RedisConnectionError("redis password"), TEMPORARY),
        (OSError(errno.ENOSPC, "private disk path"), TEMPORARY),
        (HTTPError("private URL", 403, "internal details", {}, None), SOURCE_UNAVAILABLE),
        (HTTPError("private URL", 429, "internal details", {}, None), RATE_LIMITED),
        (TelegramForbiddenError(method=MagicMock(), message="blocked"), NO_ACCESS),
        (RuntimeError("secret internal details"), captions.FAILED),
    ],
)
def test_user_messages_are_actionable_without_internal_details(error, expected):
    assert user_message(error) == expected


def test_safe_user_errors_are_escaped_and_bounded():
    text = user_message(UserError("<b>Private & unavailable</b> " + "🎬" * 1000))
    assert "<b>" not in text and "&lt;b&gt;" in text and "&amp;" in text
    assert len(text.encode("utf-16-le")) < 1200


def test_wrapped_rate_limit_preserves_advice_but_uncertainty_wins():
    from app.services.requests import DeliveryUncertain

    wrapper = delivery.DeliveryNotSent()
    wrapper.__cause__ = TelegramRetryAfter(method=MagicMock(), message="limited", retry_after=61)
    assert user_message(wrapper) == RATE_LIMITED
    unknown = DeliveryUncertain()
    unknown.__cause__ = TimeoutError()
    assert user_message(unknown) == captions.UNCERTAIN
    wrapper.__cause__ = wrapper
    assert user_message(wrapper) == captions.FAILED


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        TelegramNetworkError(method=MagicMock(), message="offline"),
        RuntimeError("bad response"),
    ],
)
def test_status_errors_never_abort_the_download(failure):
    bot = NS(edit_message_text=AsyncMock(side_effect=failure))
    asyncio.run(delivery.edit_status(bot, 1, 2, "status", False))


@pytest.mark.parametrize(
    "failure", [TimeoutError(), TelegramForbiddenError(method=MagicMock(), message="blocked")]
)
def test_failed_error_feedback_is_not_retried(failure):
    msg = NS(answer=AsyncMock(side_effect=failure))
    asyncio.run(errors.report_error(msg, RuntimeError("root cause")))
    msg.answer.assert_awaited_once()


def test_deleted_status_gets_one_fallback_but_timeout_does_not():
    msg = NS(
        edit_text=AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found")
        ),
        answer=AsyncMock(),
    )
    asyncio.run(errors.report_error(msg, UserError("removed"), edit=True))
    msg.answer.assert_awaited_once()
    msg.answer.reset_mock()
    msg.edit_text.side_effect = TimeoutError()
    asyncio.run(errors.report_error(msg, RuntimeError(), edit=True))
    msg.answer.assert_not_awaited()


def test_old_callback_does_not_abort_action_and_alert_text_is_plain_and_bounded():
    callback = NS(
        answer=AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old"))
    )
    asyncio.run(errors.acknowledge_callback(callback, user_message(UserError("<b> " * 200)), show_alert=True))
    text = callback.answer.call_args.args[0]
    assert "&lt;" not in text and len(text) <= 180


def test_dispatcher_boundary_handles_handler_and_membership_middleware_failures():
    async def run(in_middleware):
        dp, router = Dispatcher(), Router()
        dp.errors.register(errors.on_error)

        async def fail(*args, **kwargs):
            raise RuntimeError("internal private diagnostic")

        if in_middleware:
            router.message.outer_middleware(fail)
        router.message.register(fail)
        dp.include_router(router)
        bot = Bot("123456:offline-test-token")
        bot.session.make_request = AsyncMock(return_value=True)
        update = Update.model_validate(
            {
                "update_id": 100,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                    "text": "/start",
                },
            }
        )
        try:
            assert await dp.feed_update(bot, update) is True
            method = bot.session.make_request.call_args.args[1]
            assert method.text == captions.FAILED
            bot.session.make_request.assert_awaited_once()
        finally:
            await bot.session.close()

    asyncio.run(run(False))
    asyncio.run(run(True))


@pytest.mark.parametrize(
    "payload", ["invalid JSON", "null", '{"title":"missing fields"}', '{"formats":[null]}']
)
def test_corrupt_metadata_is_evicted_without_touching_journals(monkeypatch, payload):
    client = MagicMock()
    client.get.return_value = payload
    monkeypatch.setattr(cache, "client", lambda: client)
    assert cache.load_info("yt", "id") is None
    client.delete.assert_called_once_with("info:yt:id")


def test_cache_outage_is_not_treated_as_a_cache_miss(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RedisConnectionError("unavailable")
    monkeypatch.setattr(cache, "client", lambda: client)
    with pytest.raises(RedisConnectionError):
        cache.load_info("yt", "id")
    client.delete.assert_not_called()


def test_logs_redact_tokens_credentials_and_queries_in_tracebacks():
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
    stream = io.StringIO()
    logger = logging.Logger("isolated-test")
    logger.addHandler(logging.StreamHandler(stream))
    redact_logs(logger)
    redact_logs(logger)
    try:
        raise RuntimeError(
            f"https://api.telegram.org/bot{token}/sendVideo https://cdn.invalid/v?signature=private redis://user:password@redis:6379/0"
        )
    except RuntimeError:
        logger.exception("upload failed request=%s", "request-id")
    text = stream.getvalue()
    assert all(secret not in text for secret in (token, "signature=private", "user:password"))
    assert "RuntimeError" in text and "request-id" in text and "sendVideo" in text
    assert redact("https://[invalid?secret=abc") == "[invalid URL]"
