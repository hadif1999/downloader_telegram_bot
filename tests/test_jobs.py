import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.models import FormatOption, GalleryItem, MediaInfo
from app.services import cache, captions, jobs, requests
from app.services.errors import RATE_LIMITED, TEMPORARY
from app.worker import tasks


def info():
    return MediaInfo(
        "yt",
        "abcdefghijk",
        "https://youtu.be/abcdefghijk",
        "Title",
        formats=[FormatOption("720", "720p", 100, 720), FormatOption("mp3", "MP3", 100)],
    )


def bot():
    b = MagicMock()
    b.me = AsyncMock(return_value=NS(username="test_bot"))
    b.send_message = AsyncMock(return_value=NS(message_id=20))
    b.send_video = AsyncMock(return_value=NS(photo=[], video=NS(file_id="valid"), audio=None))
    return b


def card():
    c = MagicMock(message_id=20, photo=[], reply_markup=NS())
    c.edit_reply_markup = AsyncMock()
    c.edit_text = AsyncMock()
    c.delete = AsyncMock()
    return c


def test_publish_failure_restores_card_cancels_request_and_releases_lock(request_store, monkeypatch):
    publish = MagicMock(side_effect=ConnectionError("offline broker"))
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    c = card()
    assert (
        asyncio.run(jobs.request_download(bot(), info(), "720", None, chat_id=1, user_id=10, card=c))
        == TEMPORARY
    )
    assert TEMPORARY in c.edit_text.call_args.args[0]
    assert c.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    request_id = publish.call_args.kwargs["task_id"]
    assert requests.load(request_id, 10)["phase"] == "cancelled"
    assert request_store.get(requests.keys(request_id, 10)[2]) is None


def test_publish_failure_also_updates_new_status_when_no_card_was_supplied(request_store, monkeypatch):
    monkeypatch.setattr(
        tasks.download_media, "apply_async", MagicMock(side_effect=ConnectionError("broker offline"))
    )
    b, status = bot(), card()
    b.send_message.return_value = status
    assert asyncio.run(jobs.request_download(b, info(), "720", None, chat_id=1, user_id=10)) == TEMPORARY
    assert TEMPORARY in status.edit_text.call_args.args[0]
    assert status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard


def test_ambiguous_publish_claimed_by_worker_is_not_cancelled(request_store, monkeypatch):
    def publish(**kwargs):
        assert requests.claim(kwargs["task_id"], 10, "worker") == 1
        raise ConnectionError("acknowledgement lost")

    send = MagicMock(side_effect=publish)
    monkeypatch.setattr(tasks.download_media, "apply_async", send)
    assert asyncio.run(jobs.request_download(bot(), info(), "720", None, chat_id=1, user_id=10)) is None
    assert requests.load(send.call_args.kwargs["task_id"], 10)["phase"] == "working"


@pytest.mark.parametrize("phase,expected", [("done", None), ("uncertain", captions.UNCERTAIN)])
def test_lost_publish_ack_preserves_completed_worker_outcome(request_store, monkeypatch, phase, expected):
    def publish(**kwargs):
        key = kwargs["task_id"]
        requests.claim(key, 10, "worker")
        requests.transition(key, 10, "worker", phase)
        raise ConnectionError("broker acknowledgement lost after worker outcome")

    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert asyncio.run(jobs.request_download(bot(), info(), "720", None, chat_id=1, user_id=10)) == expected


def test_invalid_cached_file_is_deleted_then_queued(request_store, monkeypatch):
    cache.save_file_id("yt", "abcdefghijk", "720", None, "bad-id")
    b = bot()
    b.send_video.side_effect = TelegramBadRequest(method=MagicMock(), message="wrong file identifier")
    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert asyncio.run(jobs.request_download(b, info(), "720", None, chat_id=1, user_id=10)) is None
    assert cache.get_file_id("yt", "abcdefghijk", "720", None) is None
    publish.assert_called_once()
    key = publish.call_args.kwargs["task_id"]
    assert requests.load(key, 10)["phase"] == "queued"
    assert request_store.get(requests.keys(key, 10)[2]) == key


def test_other_cached_telegram_errors_do_not_trigger_redownload(request_store, monkeypatch):
    cache.save_file_id("yt", "abcdefghijk", "720", None, "valid-id")
    b = bot()
    b.send_video.side_effect = TelegramBadRequest(method=MagicMock(), message="chat not found")
    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert (
        asyncio.run(jobs.request_download(b, info(), "720", None, chat_id=1, user_id=10)) == captions.FAILED
    )
    publish.assert_not_called()
    assert cache.get_file_id("yt", "abcdefghijk", "720", None) == "valid-id"


def test_cached_resend_keeps_large_existing_files_usable(request_store, monkeypatch):
    cache.save_file_id("yt", "abcdefghijk", "720", None, "valid-id")
    i = info()
    i.formats[0].size = 2000
    monkeypatch.setattr(jobs, "get_settings", lambda: NS(max_file_bytes=100))
    b, c = bot(), card()
    assert asyncio.run(jobs.request_download(b, i, "720", None, chat_id=1, user_id=10, card=c)) is None
    b.send_video.assert_awaited_once()
    c.delete.assert_awaited_once()


def test_missing_format_or_language_never_queues(monkeypatch):
    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert (
        asyncio.run(jobs.request_download(bot(), info(), "1080", None, chat_id=1, user_id=10))
        == captions.UNAVAILABLE
    )
    assert (
        asyncio.run(jobs.request_download(bot(), info(), "720", "de", chat_id=1, user_id=10))
        == captions.UNAVAILABLE
    )
    publish.assert_not_called()


def test_mp3_does_not_reject_based_on_estimated_source_size(request_store, monkeypatch):
    i = info()
    i.formats[1].size = 10_000
    monkeypatch.setattr(jobs, "get_settings", lambda: NS(max_file_bytes=100, queue_ttl=1000))
    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert asyncio.run(jobs.request_download(bot(), i, "mp3", None, chat_id=1, user_id=10)) is None
    publish.assert_called_once()


def test_cache_lookup_runs_off_event_loop(request_store, monkeypatch):
    events = []

    def lookup(*a):
        events.append("redis-start")
        time.sleep(0.05)
        events.append("redis-end")
        return None

    monkeypatch.setattr(cache, "get_file_id", lookup)
    monkeypatch.setattr(tasks.download_media, "apply_async", MagicMock())

    async def run():
        async def other():
            while "redis-start" not in events:
                await asyncio.sleep(0)
            events.append("other-update")

        await asyncio.gather(
            jobs.request_download(bot(), info(), "720", None, chat_id=1, user_id=10), other()
        )

    asyncio.run(run())
    assert events.index("other-update") < events.index("redis-end")


def test_definite_cached_send_failure_leaves_no_live_request(request_store, monkeypatch):
    cache.save_file_id("yt", "abcdefghijk", "720", None, "valid-id")
    b = bot()
    b.send_video.side_effect = TelegramRetryAfter(method=MagicMock(), message="rate limited", retry_after=61)
    monkeypatch.setattr(tasks.download_media, "apply_async", MagicMock())
    assert asyncio.run(jobs.request_download(b, info(), "720", None, chat_id=1, user_id=10)) == RATE_LIMITED
    records = list(request_store.scan_iter("request:*"))
    assert len(records) == 1
    assert request_store.hget(records[0], "phase") == "cancelled"


@pytest.mark.parametrize("cached", ["[1]", '{"a":"b"}', '[[{},"id"]]'])
def test_malformed_album_cache_is_evicted_then_queued(request_store, monkeypatch, cached):
    i = info()
    i.kind, i.items = "gallery", [GalleryItem("https://cdn.invalid/a.jpg")]
    cache.save_file_id(i.platform, i.id, "all", None, cached)
    publish = MagicMock()
    monkeypatch.setattr(tasks.download_media, "apply_async", publish)
    assert asyncio.run(jobs.request_download(bot(), i, "all", None, chat_id=1, user_id=10)) is None
    assert cache.get_file_id(i.platform, i.id, "all", None) is None
    publish.assert_called_once()
