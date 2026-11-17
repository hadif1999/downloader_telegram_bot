"""Batch flow: summary card from several links / a playlist, its buttons, and the worker chain."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.bot.handlers import batch as handler
from app.models import CollectionEntry, FormatOption, MediaInfo
from app.services import batches, captions, requests
from app.worker import tasks

YT = NS(name="yt", title="YouTube", emoji="▶️")
SP = NS(name="sp", title="Spotify", emoji="🟢")


def video(media_id="aaaaaaaaaaa", title="A video"):
    return MediaInfo(
        "yt",
        media_id,
        f"https://youtu.be/{media_id}",
        title,
        duration=90,
        formats=[FormatOption("720", "720p", 1)],
    )


def album():
    entries = [
        CollectionEntry(f"https://open.spotify.com/track/{c * 22}", f"Song {c}", 200, "Artist") for c in "abc"
    ]
    return MediaInfo(
        "sp",
        "album",
        "https://open.spotify.com/album/x",
        "Album",
        uploader="Artist",
        kind="collection",
        entries=entries,
        extra={"total": 3, "audio": True},
    )


def status_message():
    status = MagicMock(spec=Message)
    status.message_id = 50
    status.edit_text = AsyncMock()
    return status


def user_message(user=7):
    message = MagicMock(spec=Message)
    message.message_id = 40
    message.from_user = NS(id=user)
    message.chat = NS(id=1)
    return message


def shown(status):
    call = status.edit_text.call_args
    markup = call.kwargs.get("reply_markup")
    buttons = [b for row in markup.inline_keyboard for b in row] if markup else []
    return call.args[0], buttons


def open_batch(results, skipped=0):
    status = status_message()
    asyncio.run(handler.open_batch(user_message(), status, results, skipped))
    return status


def test_several_links_open_one_summary_with_failures_and_quality(request_store):
    status = open_batch(
        [
            (YT, "aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", video(), None),
            (SP, "album", "https://open.spotify.com/album/x", album(), None),
            (YT, "bbbbbbbbbbb", "https://www.youtube.com/watch?v=bbbbbbbbbbb", None, "⚠️ Private video"),
        ],
        skipped=2,
    )
    text, buttons = shown(status)
    assert "🧺 <b>3 links</b>" in text and "🎬 1 video · 🎵 3 tracks" in text
    assert "1. A video · 1:30" in text and "4. Song c · 3:20" in text
    assert "Couldn't read 1 link" in text and "Private video" in text
    assert "Skipped 2 other link(s)" in text and "4</b> of 4 selected · Quality: 720p" in text
    labels = [b.text for b in buttons]
    assert labels[:4] == ["1080p", "✅ 720p", "480p", "🎵 MP3"]
    assert "📥 Download 4" in labels and "☑️ Choose items" in labels and "✖️ Cancel" in labels
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_album_is_audio_only_without_a_quality_row(request_store):
    status = open_batch([(SP, "album", "https://open.spotify.com/album/x", album(), None)])
    text, buttons = shown(status)
    assert text.startswith("💿 <b>Album</b>\n🟢 Spotify · 👤 Artist")
    assert "Quality" not in text and not any("720p" in b.text for b in buttons)
    batch_id = buttons[0].callback_data.split("|")[1]
    loaded = batches.load(batch_id)
    assert loaded.quality == "mp3" and [i.platform for i in loaded.items] == ["sp"] * 3


def test_nothing_readable_reports_every_failure(request_store):
    status = open_batch([(YT, "a", "https://youtu.be/a", None, "⚠️ <b>Gone</b>")])
    text = status.edit_text.call_args.args[0]
    assert text.startswith("⚠️ Nothing to download") and "Gone" in text and "<b>Gone" not in text


def test_items_are_deduplicated_and_capped(request_store, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "collection_max_items", 2)
    one = (YT, "aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", video(), None)
    status = open_batch([one, (SP, "album", "u", album(), None), one])
    text, _ = shown(status)
    assert "Only the first" not in text  # only collections announce their cut
    batch_id = shown(status)[1][0].callback_data.split("|")[1]
    assert len(batches.load(batch_id).items) == 2


@pytest.fixture
def card(request_store):
    status = open_batch([(SP, "album", "https://open.spotify.com/album/x", album(), None)])
    batch_id = shown(status)[1][0].callback_data.split("|")[1]
    return batch_id, status


def press(status, data, user=7):
    cq = NS(data=data, message=status, from_user=NS(id=user), answer=AsyncMock())
    asyncio.run(handler.on_batch(cq, MagicMock()))
    return cq


def test_checklist_toggles_and_pages(card):
    batch_id, status = card
    press(status, f"bt|{batch_id}|p|0")
    text, buttons = shown(status)
    assert "Tap an item" in text and buttons[0].text.startswith("✅ 1. Song a")
    press(status, f"bt|{batch_id}|t|1")
    text, buttons = shown(status)
    assert buttons[1].text.startswith("⬜ 2. Song b") and "2 of 3 selected" in text
    press(status, f"bt|{batch_id}|back")
    text, buttons = shown(status)
    assert "<s>2. Song b · 3:20</s>" in text and any(b.text == "📥 Download 2" for b in buttons)


def test_other_users_get_an_alert_and_change_nothing(card):
    batch_id, status = card
    calls = status.edit_text.call_count
    cq = press(status, f"bt|{batch_id}|t|0", user=8)
    assert "Only the person who sent" in cq.answer.call_args.args[0]
    assert status.edit_text.call_count == calls and batches.load(batch_id).chosen == [0, 1, 2]


def test_confirm_starts_the_chain_and_shows_progress(card, monkeypatch):
    batch_id, status = card
    published = MagicMock()
    monkeypatch.setattr(tasks.advance_batch, "apply_async", published)
    press(status, f"bt|{batch_id}|go")
    published.assert_called_once_with((batch_id, -1), retry=False)
    text, buttons = shown(status)
    assert "Item 1 of 3" in text and [b.text for b in buttons] == ["⏹ Stop after this item"]
    cq = press(status, f"bt|{batch_id}|go")  # double tap
    assert "already started" in cq.answer.call_args.args[0] and published.call_count == 1


def test_cancel_a_draft_from_the_card(card):
    batch_id, status = card
    press(status, f"bt|{batch_id}|x")
    assert status.edit_text.call_args.args[0].startswith("🚫 Cancelled")
    assert batches.load(batch_id).phase == "cancelled"


# --- worker chain


@pytest.fixture
def chain(card, monkeypatch):
    batch_id, _ = card
    batches.start(batch_id, 7)
    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(tasks, "make_bot", lambda **kwargs: bot)
    edits = AsyncMock()
    monkeypatch.setattr(tasks, "edit_status", edits)
    monkeypatch.setattr(
        tasks.media,
        "resolve",
        lambda platform, media_id, url: MediaInfo(
            platform, media_id, url, "Song", kind="audio", formats=[FormatOption("mp3", "MP3", None)]
        ),
    )
    return NS(id=batch_id, edits=edits)


def test_items_from_cache_or_failing_early_move_on_until_one_is_queued(chain, monkeypatch):
    outcomes = iter([(None, False), ("⚠️ Broken", False), (None, True)])
    calls = []

    async def start_download(bot, info, fmt, lang, **kwargs):
        calls.append((info.id, fmt, kwargs["batch"], kwargs["reply_to"]))
        return next(outcomes)

    monkeypatch.setattr(tasks.jobs, "start_download", start_download)
    asyncio.run(tasks._advance(chain.id, -1))
    assert [(fmt, b) for _, fmt, b, _ in calls] == [
        ("mp3", (chain.id, 0)),
        ("mp3", (chain.id, 1)),
        ("mp3", (chain.id, 2)),
    ]
    assert calls[0][3] == 40  # each item replies to the user's message
    b = batches.load(chain.id)
    assert b.results == {0: ("cached", ""), 1: ("failed", "⚠️ Broken")} and b.cursor == 2
    # the queued item's download_media continues: the last item finishes the batch
    batches.record(chain.id, 2, batches.SENT)
    asyncio.run(tasks._advance(chain.id, 2))
    final = chain.edits.call_args.args[3]
    assert "Done</b>: 2 of 3 sent" in final and "2. Song b: ⚠️ Broken" in final
    assert chain.edits.call_args.args[5] is None


def test_busy_user_retries_the_same_item_later(chain, monkeypatch):
    monkeypatch.setattr(tasks.jobs, "start_download", AsyncMock(return_value=(captions.BUSY, False)))
    with pytest.raises(batches.Busy):
        asyncio.run(tasks._advance(chain.id, -1))
    assert batches.load(chain.id).cursor == -1 and batches.begin(chain.id, -1) == 0


def test_stopped_batch_ends_after_the_current_item(chain, monkeypatch):
    start = AsyncMock(return_value=(None, True))
    monkeypatch.setattr(tasks.jobs, "start_download", start)
    asyncio.run(tasks._advance(chain.id, -1))
    batches.stop(chain.id, 7)
    batches.record(chain.id, 0, batches.SENT)  # finish_batch_item, before it advances
    asyncio.run(tasks._advance(chain.id, 0))
    assert start.await_count == 1
    assert "Stopped</b>: 1 of 3 sent" in chain.edits.call_args.args[3]
    assert "2 item(s) were not started" in chain.edits.call_args.args[3]


@pytest.mark.parametrize(
    "phase,failure,code",
    [
        ("done", None, "sent"),
        ("cancelled", None, "skipped"),
        ("uncertain", None, "uncertain"),
        ("failed", RuntimeError("x"), "failed"),
    ],
)
def test_finished_item_is_recorded_and_the_next_one_is_requested(chain, monkeypatch, phase, failure, code):
    monkeypatch.setattr(requests, "load", lambda *a: {"phase": phase})
    published = MagicMock()
    monkeypatch.setattr(tasks.advance_batch, "apply_async", published)
    tasks.finish_batch_item(chain.id, 0, "request", 7, failure)
    assert batches.load(chain.id).outcome(0) == code
    published.assert_called_once_with((chain.id, 0), retry=False)


def test_download_task_continues_the_batch_only_when_finished(monkeypatch):
    finished = MagicMock()
    monkeypatch.setattr(tasks, "finish_batch_item", finished)

    async def busy(*args):
        raise requests.LeaseBusy

    monkeypatch.setattr(tasks, "_run", busy)
    monkeypatch.setattr(tasks.download_media, "retry", MagicMock(side_effect=RuntimeError("retry")))
    with pytest.raises(RuntimeError):
        tasks.download_media(1, 2, False, "yt", "id", "720", None, 7, batch=["b", 3])
    finished.assert_not_called()  # a redelivery race retries; the batch waits for the real end

    async def done(*args):
        return None

    monkeypatch.setattr(tasks, "_run", done)
    tasks.download_media(1, 2, False, "yt", "id", "720", None, 7, batch=["b", 3])
    assert finished.call_args.args[:2] == ("b", 3)
