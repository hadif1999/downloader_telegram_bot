import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.handlers import links
from app.models import FormatOption, MediaInfo
from app.platforms.ytdlp import UserError
from app.services import captions, jobs
from app.worker import tasks


@pytest.fixture
def link_request(monkeypatch):
    status = MagicMock(message_id=20)
    status.chat.id = 1
    status.edit_text = AsyncMock()
    message = MagicMock(message_id=10, text="https://example.com/video", caption=None)
    message.chat.id = 1
    message.reply = AsyncMock(return_value=status)
    platform = SimpleNamespace(name="yt", title="YouTube")
    info = MediaInfo("yt", "id", message.text, "Title", formats=[FormatOption("720", "720p", None)])
    monkeypatch.setattr(links.registry, "detect", lambda _: (platform, "id", message.text))
    monkeypatch.setattr(links, "_extract_sem", asyncio.Semaphore(4))
    monkeypatch.setattr(links, "edit_status", AsyncMock())
    monkeypatch.setattr(links, "send_info_card", AsyncMock())
    monkeypatch.setattr(links, "request_download", AsyncMock(return_value=None))

    def resolve(*args):
        message.reply.assert_awaited_once_with(captions.EXTRACT_WAITING)
        return info

    monkeypatch.setattr(links.media, "resolve", resolve)
    return message, status, info


def test_acknowledges_before_extraction_and_reuses_status_for_download(link_request):
    message, status, _ = link_request
    asyncio.run(links.on_link(message))
    assert links.request_download.call_args.kwargs["card"] is status
    links.edit_status.assert_awaited_once()
    links.send_info_card.assert_not_awaited()


def test_format_card_reuses_status(link_request):
    message, status, info = link_request
    info.formats.append(FormatOption("1080", "1080p", None))
    asyncio.run(links.on_link(message))
    links.send_info_card.assert_awaited_once_with(status, info)
    links.request_download.assert_not_awaited()


@pytest.mark.parametrize("error", [UserError("Private post"), RuntimeError("offline")])
def test_extraction_failure_replaces_pending_status(monkeypatch, link_request, error):
    message, status, _ = link_request
    monkeypatch.setattr(links.media, "resolve", MagicMock(side_effect=error))
    asyncio.run(links.on_link(message))
    assert status.edit_text.call_args.args[0].startswith("⚠️")
    assert message.reply.await_count == 1
    links.request_download.assert_not_awaited()


def test_busy_download_replaces_pending_status(link_request):
    message, status, _ = link_request
    links.request_download.return_value = captions.BUSY
    asyncio.run(links.on_link(message))
    status.edit_text.assert_awaited_once_with(captions.BUSY)


def test_failed_delivery_replaces_pending_status(link_request):
    message, status, _ = link_request
    links.request_download.side_effect = RuntimeError("Telegram unavailable")
    asyncio.run(links.on_link(message))
    status.edit_text.assert_awaited_once_with(captions.FAILED, request_timeout=5)


@pytest.mark.parametrize("fails", [False, True])
def test_extraction_heartbeat_stops_before_delivery(monkeypatch, link_request, fails):
    message, _, info = link_request

    async def check():
        stopped = asyncio.Event()

        async def heartbeat(*args):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def resolve(*args):
            await asyncio.sleep(0)
            if fails:
                raise RuntimeError("offline")
            return info

        monkeypatch.setattr(links, "extraction_progress", heartbeat)
        monkeypatch.setattr(links.asyncio, "to_thread", resolve)
        await links.on_link(message)
        assert stopped.is_set()

    asyncio.run(check())


@pytest.mark.parametrize("extraction_fails", [False, True])
def test_failed_heartbeat_does_not_replace_extraction_outcome(monkeypatch, link_request, extraction_fails):
    message, status, info = link_request

    async def heartbeat(*args):
        raise RuntimeError("progress failed")

    async def resolve(*args):
        await asyncio.sleep(0)
        if extraction_fails:
            raise UserError("Original extraction error")
        return info

    monkeypatch.setattr(links, "extraction_progress", heartbeat)
    monkeypatch.setattr(links.asyncio, "to_thread", resolve)
    asyncio.run(links.on_link(message))
    if extraction_fails:
        assert "Original extraction error" in status.edit_text.call_args.args[0]
        links.request_download.assert_not_awaited()
    else:
        links.request_download.assert_awaited_once()


def test_waiting_for_extraction_slot_is_acknowledged(monkeypatch, link_request):
    message, _, info = link_request

    async def check():
        semaphore = asyncio.Semaphore(0)
        monkeypatch.setattr(links, "_extract_sem", semaphore)
        resolve = MagicMock(return_value=info)
        monkeypatch.setattr(links.media, "resolve", resolve)
        task = asyncio.create_task(links.on_link(message))
        await asyncio.sleep(0)
        message.reply.assert_awaited_once_with(captions.EXTRACT_WAITING)
        resolve.assert_not_called()
        semaphore.release()
        await task

    asyncio.run(check())


def test_slow_extraction_reports_elapsed_time(monkeypatch):
    status = MagicMock(message_id=20)
    edit = AsyncMock()
    monkeypatch.setattr(links, "edit_status", edit)
    clock = MagicMock(side_effect=[0, 5])
    monkeypatch.setattr(
        links,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=clock),
            sleep=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(links.extraction_progress(status, "YouTube"))
    assert "5s elapsed" in edit.call_args.args[3]


@pytest.mark.parametrize("thumbnail_rejected", [False, True])
def test_status_becomes_photo_card_or_text_fallback(monkeypatch, thumbnail_rejected, tmp_path):
    status = MagicMock()
    status.edit_media = AsyncMock()
    status.edit_text = AsyncMock()
    if thumbnail_rejected:
        status.edit_media.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad thumbnail")
    info = MediaInfo("yt", "id", "url", "Title", thumbnail="https://example.com/thumb.jpg")

    def download(url, path, **kwargs):
        path.write_bytes(b"thumbnail")
        return path

    monkeypatch.setattr(links.media, "download_file", download)
    asyncio.run(links.send_info_card(status, info))
    status.edit_media.assert_awaited_once()
    assert status.edit_text.await_count == int(thumbnail_rejected)


def test_text_status_without_buttons_can_be_queued(monkeypatch, request_store):
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(username="bot"))
    card = MagicMock(message_id=20, reply_markup=None, photo=[])
    card.edit_reply_markup = AsyncMock(side_effect=AssertionError("No markup to remove"))
    card.edit_text = AsyncMock()
    monkeypatch.setattr(jobs.cache, "get_file_id", lambda *args: None)
    enqueue = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", enqueue)
    info = MediaInfo("yt", "id", "url", "Title", formats=[FormatOption("720", "720p", None)])
    assert asyncio.run(jobs.request_download(bot, info, "720", None, chat_id=1, user_id=2, card=card)) is None
    enqueue.assert_called_once()
    card.edit_reply_markup.assert_not_awaited()


def test_cached_download_removes_pending_status(monkeypatch, request_store):
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(username="bot"))
    card = MagicMock()
    card.delete = AsyncMock()
    monkeypatch.setattr(jobs.cache, "get_file_id", lambda *args: "cached-id")
    send = AsyncMock()
    monkeypatch.setattr(jobs, "send_cached", send)
    info = MediaInfo("yt", "id", "url", "Title", formats=[FormatOption("720", "720p", None)])
    asyncio.run(jobs.request_download(bot, info, "720", None, chat_id=1, user_id=2, card=card))
    send.assert_awaited_once()
    card.delete.assert_awaited_once()
