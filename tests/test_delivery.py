import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

from app.services import delivery, requests


def message(kind="photo", file_id="fid"):
    return NS(
        photo=[NS(file_id=file_id)] if kind == "photo" else [],
        video=NS(file_id=file_id) if kind == "video" else None,
        audio=NS(file_id=file_id) if kind == "audio" else None,
        document=None,
    )


def bot():
    b = MagicMock()
    b.send_photo = AsyncMock(return_value=message())
    b.send_video = AsyncMock(return_value=message("video"))

    async def album(chat_id, group, **kwargs):
        assert 2 <= len(group) <= 10
        return [
            message("video" if item.type == "video" else "photo", f"fid-{index}")
            for index, item in enumerate(group)
        ]

    b.send_media_group = AsyncMock(side_effect=album)
    return b


@pytest.mark.parametrize("count", [1, 2, 10, 11, 21, 31, 41, 50])
def test_gallery_delivers_every_item_with_valid_album_sizes(count):
    b = bot()
    result = asyncio.run(delivery.send_gallery(b, 1, [("file", "photo")] * count, "caption"))
    assert len(result) == count
    assert b.send_photo.await_count == int(count % 10 == 1)


def test_singleton_video_uses_video_method():
    b = bot()
    asyncio.run(delivery.send_gallery(b, 1, [("file", "video")], "caption"))
    b.send_video.assert_awaited_once()
    b.send_media_group.assert_not_awaited()


def test_partial_gallery_resumes_after_recorded_batch_without_duplicates(request_store):
    async def run():
        requests.create("a", 10)
        requests.claim("a", 10, "owner")
        prefix = [["photo", f"old-{i}"] for i in range(10)]
        requests.transition("a", 10, "owner", "ready", prefix, 11)
        journal = await delivery.Journal.load("a", 10, "owner")
        b = bot()
        await delivery.send_gallery(b, 1, [("file", "photo")] * 11, "caption", journal=journal)
        assert len(journal.sent) == 11 and journal.sent[:10] == prefix
        b.send_media_group.assert_not_awaited()
        b.send_photo.assert_awaited_once()
        assert b.send_photo.call_args.kwargs["caption"] is None

    asyncio.run(run())


def test_rate_limit_retries_same_upload_with_bound(monkeypatch):
    send = AsyncMock(
        side_effect=[TelegramRetryAfter(method=MagicMock(), message="slow down", retry_after=1), [message()]]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    assert asyncio.run(delivery.retry_send(send))
    assert send.await_count == 2
    sleep.assert_awaited_once_with(2)


def test_exhausted_rate_limit_reports_definite_non_delivery(monkeypatch):
    send = AsyncMock(side_effect=TelegramRetryAfter(method=MagicMock(), message="slow down", retry_after=1))
    monkeypatch.setattr(delivery.asyncio, "sleep", AsyncMock())
    with pytest.raises(delivery.DeliveryNotSent):
        asyncio.run(delivery.retry_send(send))
    assert send.await_count == 3


def test_ambiguous_timeout_is_not_retried_and_persists_uncertainty(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "owner")
    send = AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message="response timeout"))
    with pytest.raises(requests.DeliveryUncertain):
        asyncio.run(delivery.Journal("a", 10, "owner").send(send))
    assert send.await_count == 1
    assert requests.load("a", 10)["phase"] == "uncertain"


def test_unknown_response_error_is_uncertain_not_safe_to_resend(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "owner")
    send = AsyncMock(side_effect=RuntimeError("malformed response after upload"))
    with pytest.raises(requests.DeliveryUncertain):
        asyncio.run(delivery.Journal("a", 10, "owner").send(send))
    send.assert_awaited_once()
    assert requests.load("a", 10)["phase"] == "uncertain"


def test_rejected_file_id_does_not_commit_a_batch(request_store):
    requests.create("a", 10)
    requests.claim("a", 10, "owner")
    send = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="wrong file identifier"))
    journal = delivery.Journal("a", 10, "owner")
    with pytest.raises(TelegramBadRequest):
        asyncio.run(journal.send(send))
    assert journal.sent == [] and requests.load("a", 10)["phase"] == "ready"


def test_cloud_api_never_receives_local_file_uri(monkeypatch):
    from aiogram.types import FSInputFile

    monkeypatch.setattr(delivery, "get_settings", lambda: NS(bot_api_url="", bot_api_local=True))
    assert isinstance(delivery.local_file("/tmp/file.mp4"), FSInputFile)


def test_connect_failure_retries_but_response_timeout_does_not(monkeypatch):
    from aiohttp import ClientConnectorError

    failure = TelegramNetworkError(method=MagicMock(), message="connection failed")
    failure.__cause__ = ClientConnectorError(MagicMock(), ConnectionRefusedError())
    send = AsyncMock(side_effect=[failure, [message()]])
    monkeypatch.setattr(delivery.asyncio, "sleep", AsyncMock())
    assert asyncio.run(delivery.retry_send(send))
    assert send.await_count == 2


@pytest.mark.parametrize("url", ["", "http://telegram-bot-api:8081", "http://127.0.0.1:8081"])
def test_bot_session_supports_cloud_and_trusted_local_api(monkeypatch, url):
    from app.config import Settings
    from app.services import telegram

    settings = Settings(_env_file=None, bot_api_url=url, bot_api_local=bool(url))
    monkeypatch.setattr(telegram, "get_settings", lambda: settings)
    b = telegram.make_bot()
    assert b.id == 123456
    assert b.session.timeout == 1800
    if url:
        assert b.session.api.base.startswith(url)
    asyncio.run(b.session.close())
