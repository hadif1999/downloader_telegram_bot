"""Inline mode: result lists, starting a chosen result once, and delivery onto the inline card."""

import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
)

from app.bot import activity, membership
from app.bot.handlers import callbacks, start
from app.bot.handlers import inline as handler
from app.config import Settings
from app.models import DownloadResult, FormatOption, GalleryItem, MediaInfo
from app.services import cache, captions, inline, ops, requests, stats
from app.worker import tasks

MB = 1 << 20


def video(**extra):
    return MediaInfo(
        "yt",
        "abc",
        "https://youtu.be/abcdefghijk",
        "Title",
        uploader="Creator",
        duration=213,
        thumbnail="https://i.ytimg.com/vi/abc/hq.jpg",
        formats=[
            FormatOption("1080", "1080p", 5000 * MB, 1080),
            FormatOption("720", "720p", 48 * MB, 720),
            FormatOption("mp3", "MP3", 5 * MB),
        ],
        **extra,
    )


def bad_request(text="Bad Request: message to edit not found"):
    return TelegramBadRequest(method=MagicMock(), message=text)


# --- results


def test_results_offer_each_fitting_format_cached_ones_instant(request_store):
    info = video()
    results = inline.build_results(10, info, {"720": "video-fid"}, "test_bot")
    assert [type(r) for r in results] == [InlineQueryResultCachedVideo, InlineQueryResultArticle]
    cached, mp3 = results
    assert cached.video_file_id == "video-fid" and "instant" in cached.title
    assert "Downloaded by @test_bot" in cached.caption
    assert mp3.title.startswith("🎵 MP3 · 5.0MB")
    assert mp3.thumbnail_url == info.thumbnail
    assert mp3.input_message_content.link_preview_options.url == info.thumbnail
    assert mp3.reply_markup.inline_keyboard[0][0].callback_data == "inl|" + mp3.id
    assert all(len(r.id.encode()) <= 64 for r in results)
    # 1080p is over the upload limit and is not offered
    assert not any("1080p" in getattr(r, "title", "") for r in results)
    assert inline.more_button(info, True) is None


def test_result_ids_are_personal_and_resolve_to_the_choice(request_store):
    info = video()
    inline.save_choices(10, info, ["720", "mp3"])
    rid = inline.result_id(10, "yt", "abc", "720")
    assert rid != inline.result_id(11, "yt", "abc", "720")
    assert inline.load_choice(rid) == ("yt", "abc", "720", 10)
    assert inline.load_choice("unknown") is None


def test_playlists_albums_and_dubbed_audio_point_to_the_bot(request_store):
    playlist = MediaInfo("yt", "pl", "https://youtube.com/playlist?list=x", "List", kind="collection")
    assert inline.offered(playlist, 2000 * MB) == []
    button = inline.more_button(playlist, False)
    token = button.start_parameter.removeprefix("l_")
    assert inline.link_for_token(token) == playlist.url
    assert len(button.start_parameter) <= 64

    album = MediaInfo("ig", "p", "https://instagram.com/p/x", "Post", kind="gallery")
    album.items = [GalleryItem("https://a/1.jpg"), GalleryItem("https://a/2.jpg")]
    assert inline.offered(album, 2000 * MB) == []
    assert "album" in inline.more_button(album, False).text

    single = MediaInfo("ig", "q", "https://instagram.com/p/y", "Post", kind="gallery")
    single.items = [GalleryItem("https://a/1.jpg")]
    assert [f.key for f in inline.offered(single, 2000 * MB)] == ["all"]
    assert inline.cached_media("all", json.dumps([["photo", "pid"]])) == ("photo", "pid")
    assert inline.cached_media("all", json.dumps([["photo", "a"], ["photo", "b"]])) is None


# --- answering queries


def query(text, user_id=10):
    q = MagicMock()
    q.query, q.from_user = text, NS(id=user_id, is_bot=False)
    q.answer = AsyncMock()
    return q


def test_query_without_a_link_shows_how_to_use():
    q = query("hello")
    asyncio.run(handler.on_inline_query(q, MagicMock()))
    results = q.answer.await_args.args[0]
    assert results == [] and q.answer.await_args.kwargs["button"].start_parameter == "inline"


def test_slow_lookup_answers_still_looking_and_runs_once(monkeypatch, request_store):
    calls = []

    async def slow(platform, media_id, url=None):
        calls.append(media_id)
        await asyncio.sleep(0.2)
        return video()

    monkeypatch.setattr(handler.media, "resolve_async", slow)

    async def scenario():
        first = handler.lookup("yt", "abc", "u", 0.01)
        second = handler.lookup("yt", "abc", "u", 0.01)
        assert await asyncio.gather(first, second) == [None, None]
        info = await handler.lookup("yt", "abc", "u", 1)
        return info

    assert asyncio.run(scenario()).id == "abc"
    assert calls == ["abc"]


def test_query_lists_results_personal_and_briefly_cached(monkeypatch, request_store):
    async def resolved(platform, media_id, url=None):
        return video()

    monkeypatch.setattr(handler.media, "resolve_async", resolved)
    q = query("https://youtu.be/abcdefghijk")
    b = MagicMock()
    b.me = AsyncMock(return_value=NS(username="test_bot"))
    asyncio.run(handler.on_inline_query(q, b))
    results = q.answer.await_args.args[0]
    assert len(results) == 2
    assert q.answer.await_args.kwargs["is_personal"] is True
    assert q.answer.await_args.kwargs["cache_time"] <= 10
    assert inline.load_choice(results[0].id)[3] == 10


# --- starting a chosen result


@pytest.fixture
def chosen(request_store, monkeypatch):
    info = video()
    inline.save_choices(10, info, ["720"])
    rid = inline.result_id(10, "yt", "abc", "720")

    async def resolved(platform, media_id, url=None):
        return info

    monkeypatch.setattr(handler.media, "resolve_async", resolved)
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "request_download", download)
    edit = AsyncMock()
    monkeypatch.setattr(handler, "edit_status", edit)
    b = MagicMock()
    b.me = AsyncMock(return_value=NS(username="test_bot"))
    b.edit_message_media = AsyncMock()
    return NS(info=info, rid=rid, download=download, edit=edit, bot=b)


def test_chosen_result_and_start_button_start_one_download(chosen):
    assert asyncio.run(handler.begin(chosen.bot, 10, chosen.rid, "IM")) is None
    assert asyncio.run(handler.begin(chosen.bot, 10, chosen.rid, "IM")) == captions.INLINE_STARTED
    chosen.download.assert_awaited_once()
    assert chosen.download.await_args.kwargs == {"chat_id": 10, "user_id": 10, "inline_id": "IM"}


def test_only_the_requester_can_start(chosen):
    assert asyncio.run(handler.begin(chosen.bot, 11, chosen.rid, "IM")) == captions.INLINE_NOT_YOURS
    chosen.download.assert_not_awaited()


def test_busy_user_gets_the_start_button_back(chosen):
    chosen.download.return_value = captions.BUSY
    asyncio.run(handler.begin(chosen.bot, 10, chosen.rid, "IM"))
    edit = chosen.edit.await_args
    assert captions.BUSY in edit.args[3] and edit.kwargs["inline_id"] == "IM"
    assert edit.args[5].inline_keyboard[0][0].callback_data == "inl|" + chosen.rid
    asyncio.run(handler.begin(chosen.bot, 10, chosen.rid, "IM"))  # the claim was given back
    assert chosen.download.await_count == 2


def test_format_cached_meanwhile_is_put_on_the_card_without_a_download(chosen):
    cache.save_file_id("yt", "abc", "720", None, "video-fid")
    asyncio.run(handler.begin(chosen.bot, 10, chosen.rid, "IM"))
    chosen.download.assert_not_awaited()
    call = chosen.bot.edit_message_media.await_args.kwargs
    assert call["inline_message_id"] == "IM" and call["media"].media == "video-fid"


def test_cached_result_sent_by_telegram_is_counted(request_store, monkeypatch):
    inline.save_choices(10, video(), ["720"])
    rid = inline.result_id(10, "yt", "abc", "720")
    record = MagicMock()
    monkeypatch.setattr(stats, "record_download", record)
    asyncio.run(
        handler.on_chosen(NS(inline_message_id=None, result_id=rid, from_user=NS(id=10)), MagicMock())
    )
    record.assert_called_once_with("yt", "cached", 10)


# --- worker delivery onto the card


@pytest.fixture
def worker(request_store, monkeypatch, tmp_path):
    info = video()
    b = MagicMock()
    b.get_me = AsyncMock(return_value=NS(username="test_bot"))
    b.session.close = AsyncMock()
    b.delete_message = AsyncMock()
    b.send_video = AsyncMock(return_value=NS(photo=[], video=NS(file_id="video-id"), audio=None))
    b.edit_message_media = AsyncMock()
    settings = NS(
        download_dir=tmp_path,
        max_file_bytes=1000,
        download_retries=0,
        download_timeout=90,
        inline_storage_chat=-100777,
    )
    monkeypatch.setattr(tasks, "make_bot", lambda **kwargs: b)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks.media, "resolve", lambda *a: info)
    monkeypatch.setattr(tasks, "make_thumb", lambda *a: None)
    edit = AsyncMock()
    monkeypatch.setattr(tasks, "edit_status", edit)
    monkeypatch.setattr("app.services.delivery.asyncio.sleep", AsyncMock())

    def download(info, fmt, lang, dest, progress):
        p = dest / "media.mp4"
        p.write_bytes(b"video")
        return DownloadResult(path=str(p))

    download = MagicMock(side_effect=download)
    monkeypatch.setattr(tasks.registry, "get", lambda *a: NS(download=download))
    assert requests.create("a", 10)
    return NS(bot=b, info=info, edit=edit, download=download, settings=settings)


def run_inline():
    return asyncio.run(tasks._run(10, 0, False, "yt", "abc", "720", None, None, 10, "a", inline_id="IM"))


def test_upload_goes_to_storage_then_becomes_the_inline_card(worker):
    run_inline()
    assert worker.bot.send_video.await_args.kwargs["chat_id"] == -100777
    call = worker.bot.edit_message_media.await_args.kwargs
    assert call["inline_message_id"] == "IM" and call["media"].media == "video-id"
    # the music button reopens inline mode with the same link
    assert call["reply_markup"].inline_keyboard[0][0].switch_inline_query_current_chat == worker.info.url
    state = requests.load("a", 10)
    assert state["phase"] == "done" and state["step"] == "edited"
    worker.bot.delete_message.assert_not_awaited()
    assert all(c.kwargs.get("inline_id") == "IM" for c in worker.edit.await_args_list)
    assert cache.get_file_id("yt", "abc", "720", None) == "video-id"


def test_refused_card_falls_back_to_the_private_chat(worker):
    worker.bot.edit_message_media.side_effect = bad_request()
    run_inline()
    pm = worker.bot.send_video.await_args_list[-1]
    assert pm.args[:2] == (10, "video-id")
    assert requests.load("a", 10)["step"] == "pm"
    assert captions.INLINE_SENT_PM in worker.edit.await_args.args[3]


def test_user_who_never_started_the_bot_is_told_to_press_start(worker):
    worker.bot.edit_message_media.side_effect = bad_request()
    stored = NS(photo=[], video=NS(file_id="video-id"), audio=None)
    worker.bot.send_video.side_effect = [
        stored,
        TelegramForbiddenError(method=MagicMock(), message="blocked"),
    ]
    run_inline()
    assert requests.load("a", 10)["step"] == "start"
    last = worker.edit.await_args
    assert captions.INLINE_PRESS_START in last.args[3]
    url = last.args[5].inline_keyboard[0][0].url
    rid = url.rsplit("g_", 1)[1]
    assert inline.load_choice(rid) == ("yt", "abc", "720", 10)


def test_unconfirmed_edit_is_uncertain_and_never_sent_privately(worker):
    worker.bot.edit_message_media.side_effect = TelegramNetworkError(method=MagicMock(), message="lost")
    with pytest.raises(requests.DeliveryUncertain):
        run_inline()
    assert worker.bot.edit_message_media.await_count == 3  # the edit is idempotent: retried
    assert worker.bot.send_video.await_count == 1  # only the storage upload
    assert requests.load("a", 10)["phase"] == "uncertain"


def stored_before_crash(step):
    assert requests.claim("a", 10, "old") == 1
    requests.transition("a", 10, "old", "ready", [["video", "video-id"]], 1)
    requests.set_step("a", 10, "old", step)
    requests.release("a", 10, "old")


def test_replay_after_storage_upload_only_edits(worker):
    stored_before_crash("stored")
    run_inline()
    worker.download.assert_not_called()
    worker.bot.send_video.assert_not_awaited()
    assert worker.bot.edit_message_media.await_args.kwargs["media"].media == "video-id"
    assert requests.load("a", 10)["phase"] == "done"


def test_replay_after_a_private_send_attempt_never_repeats_it(worker):
    stored_before_crash("pm_sending")
    worker.bot.edit_message_media.side_effect = bad_request()
    with pytest.raises(requests.DeliveryUncertain):
        run_inline()
    worker.bot.send_video.assert_not_awaited()
    assert requests.load("a", 10)["phase"] == "uncertain"


def test_cancelled_inline_download_offers_the_formats_again(worker, monkeypatch):
    def cancelled(*args):
        raise tasks.progress.Cancelled

    worker.download.side_effect = cancelled
    run_inline()
    last = worker.edit.await_args
    assert captions.INLINE_CANCELLED in last.args[3]
    assert last.args[5].inline_keyboard[0][0].switch_inline_query_current_chat == worker.info.url


def test_queued_inline_cancel_redraws_the_inline_card(request_store, monkeypatch):
    info = video()
    assert requests.create("q", 10, "yt|abc|720|")

    async def resolved(platform, media_id, url=None):
        return info

    monkeypatch.setattr(callbacks.media, "resolve_async", resolved)
    edit = AsyncMock()
    monkeypatch.setattr(callbacks, "edit_status", edit)
    cq = NS(message=None, inline_message_id="IM", from_user=NS(id=10), bot=MagicMock())
    asyncio.run(callbacks._show_cancelled(cq, "q"))
    assert edit.await_args.kwargs["inline_id"] == "IM"
    assert captions.INLINE_CANCELLED in edit.await_args.args[3]


def test_admin_queue_reads_inline_tasks():
    body = json.dumps([[10, 0, False, "yt", "abc", "720", None, 10, None], {"inline": "IM"}, {}])
    task = ops._decode(json.dumps({"headers": {"task": "download_media", "id": "r"}, "body": body}))
    assert task.inline == "IM" and task.user == 10


# --- access rules


@pytest.fixture
def flags(monkeypatch):
    monkeypatch.setattr(activity, "_record", lambda event: None)
    monkeypatch.setattr(activity, "get_settings", lambda: NS(admin_ids=[]))
    saved = (activity.flags.banned, activity.flags.maintenance, activity.flags.loaded)
    activity.flags.loaded = time.monotonic()
    yield activity.flags
    activity.flags.banned, activity.flags.maintenance, activity.flags.loaded = saved


def inline_query_event(user_id=10):
    from aiogram.types import InlineQuery, User

    event = MagicMock(spec=InlineQuery)
    event.from_user = User(id=user_id, is_bot=False, first_name="T")
    event.answer = AsyncMock()
    return event


def test_banned_users_get_no_inline_results(flags):
    flags.banned, flags.maintenance = {10}, None
    event, h = inline_query_event(), AsyncMock()
    asyncio.run(activity.ActivityMiddleware()(h, event, {}))
    h.assert_not_awaited()
    assert event.answer.await_args.args[0] == [] and event.answer.await_args.kwargs["button"] is None


def test_maintenance_shows_a_notice_above_empty_results(flags):
    flags.banned, flags.maintenance = set(), "default"
    event, h = inline_query_event(), AsyncMock()
    asyncio.run(activity.ActivityMiddleware()(h, event, {}))
    h.assert_not_awaited()
    assert "maintenance" in event.answer.await_args.kwargs["button"].text


def test_missing_channel_shows_join_button_and_membership_is_remembered():
    channel = membership.Channel(-100123, "Channel", "https://t.me/channel")
    gate = membership.MembershipMiddleware((channel,), "test_bot")
    b = MagicMock()
    b.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.LEFT, is_member=False))
    event, h = inline_query_event(), AsyncMock()
    asyncio.run(gate(h, event, {"bot": b}))
    h.assert_not_awaited()
    assert event.answer.await_args.kwargs["button"].start_parameter == "join"

    b.get_chat_member.return_value = NS(status=ChatMemberStatus.MEMBER, is_member=True)
    gate._inline_seen.clear()
    for _ in range(3):
        asyncio.run(gate(h, inline_query_event(), {"bot": b}))
    assert h.await_count == 3
    assert b.get_chat_member.await_count == 2  # one check per minute while typing


# --- deep links and settings


def test_open_in_bot_deep_link_runs_the_link_flow(request_store, monkeypatch):
    token = inline.link_token("https://youtu.be/abcdefghijk")
    handled = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.links.handle_links", handled)
    message = MagicMock()
    message.chat = NS(type=ChatType.PRIVATE, id=10)
    message.from_user = NS(id=10)
    message.answer = AsyncMock()
    asyncio.run(start.start(message, MagicMock(), NS(args="l_" + token)))
    handled.assert_awaited_once_with(message, "https://youtu.be/abcdefghijk")

    asyncio.run(start.start(message, MagicMock(), NS(args="l_unknown")))
    message.answer.assert_awaited_with(captions.EXPIRED)


def test_resend_deep_link_belongs_to_its_user(request_store, monkeypatch):
    inline.save_choices(10, video(), ["720"])
    rid = inline.result_id(10, "yt", "abc", "720")
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(start, "request_download", download)
    message = MagicMock()
    message.chat = NS(type=ChatType.PRIVATE, id=11)
    message.from_user = NS(id=11)
    message.answer = AsyncMock()
    asyncio.run(start.start(message, MagicMock(), NS(args="g_" + rid)))
    message.answer.assert_awaited_with(captions.EXPIRED)
    download.assert_not_awaited()


def test_storage_chat_setting_is_validated():
    assert Settings(inline_storage_chat_id="-1001234").inline_storage_chat == -1001234
    assert Settings(inline_storage_chat_id="@store_box").inline_storage_chat == "@store_box"
    assert Settings().inline_storage_chat is None
    with pytest.raises(ValueError):
        Settings(inline_storage_chat_id="not a chat")


def test_storage_channel_needs_posting_rights(monkeypatch):
    monkeypatch.setattr(handler, "get_settings", lambda: NS(inline_storage_chat=-100777))
    b = MagicMock()
    b.me = AsyncMock(return_value=NS(id=1))
    b.get_chat = AsyncMock(return_value=NS(id=-100777, type=ChatType.CHANNEL))
    b.get_chat_member = AsyncMock(
        return_value=NS(status=ChatMemberStatus.ADMINISTRATOR, can_post_messages=False)
    )
    with pytest.raises(RuntimeError, match="INLINE_STORAGE_CHAT_ID"):
        asyncio.run(handler.check_storage(b))
    b.get_chat_member.return_value = NS(status=ChatMemberStatus.ADMINISTRATOR, can_post_messages=True)
    asyncio.run(handler.check_storage(b))


def test_without_storage_channel_a_user_who_never_started_gets_the_start_link(worker):
    worker.settings.inline_storage_chat = None  # the copy would go to the user's private chat
    worker.bot.send_video.side_effect = TelegramForbiddenError(method=MagicMock(), message="blocked")
    with pytest.raises(tasks.NeedsStart):
        run_inline()
    assert worker.bot.send_video.await_args.kwargs["chat_id"] == 10
    worker.bot.edit_message_media.assert_not_awaited()
    last = worker.edit.await_args
    assert captions.INLINE_PRESS_START in last.args[3]
    assert "start=g_" in last.args[5].inline_keyboard[0][0].url
    assert requests.load("a", 10)["phase"] == "failed"  # nothing was sent: the link starts afresh
