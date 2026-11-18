import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree as ET

import pytest
from aiogram.types import Message

from app.bot.handlers import callbacks
from app.models import FormatOption, MediaInfo
from app.services import captions, media
from app.services.progress import Monitor


def card(done, total, unit="bytes"):
    """The live download status as the worker renders it."""
    monitor = Monitor(unit)
    monitor(done, total)
    return monitor.render()


@pytest.mark.parametrize("value", ["<>&\"'" * 500, "😀" * 1500, "line\n" * 500])
def test_all_captions_are_complete_html_within_visible_limit(value):
    info = MediaInfo(
        "yt",
        "id",
        "https://example.com/?x=" + "&" * 2200,
        value,
        uploader=value,
        uploader_handle=value,
        uploader_url="https://example.com/",
        category=value,
        upload_date=value,
        formats=[FormatOption("720", value, 100, 720), FormatOption("mp3", "MP3", 50)],
    )
    for caption in (
        captions.info_caption(info, 1000),
        captions.status_caption(info, captions.QUEUED),
        captions.result_caption(info, "a" * 2000),
    ):
        root = ET.fromstring("<root>" + caption + "</root>")
        assert len("".join(root.itertext()).encode("utf-16-le")) // 2 <= 1024


def test_description_chunks_preserve_text_and_unicode():
    text = "😀" * 3999 + "<>&" * 4000
    chunks = list(captions.text_chunks(text))
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-16-le")) // 2 <= 4000 for c in chunks)


def test_download_status_puts_progress_before_compact_media_context():
    info = MediaInfo(
        "yt",
        "id",
        "https://example.com/video",
        "A title <with> characters",
        uploader="Steve Brunton",
        duration=1058,
        subscribers=565000,
        views=121734,
        likes=2439,
        comments=107,
        category="Science & Technology",
        upload_date="2022-01-28",
        formats=[FormatOption("1080", "1080p", None, 1080)],
    )
    text = captions.status_caption(info, card(40, 100), "1080", "de-DE")
    assert text.startswith("⬇️ <b>Downloading</b>")
    assert text.index("40%") < text.index("A title")
    assert "Steve Brunton · 1080p · DE-DE · 17:38" in text
    assert "565,000" not in text and "121,734" not in text
    assert "Science &amp; Technology" not in text and "2022-01-28" not in text
    ET.fromstring("<root>" + text + "</root>")


@pytest.mark.parametrize("total", [None, 0, -1])
def test_preparing_download_has_useful_feedback_without_unknown_size_marker(total):
    text = card(0, total)
    assert "Preparing download" in text and "Waiting for the first bytes" in text
    assert "?" not in text and "%" not in text


def test_unknown_total_shows_real_received_bytes_without_invented_percentage():
    text = card(2 * 1024 * 1024, None)
    assert "<b>2.0MB</b> received" in text
    assert "Total size not provided" in text
    assert "?" not in text and "%" not in text and "<code>" not in text


def test_unknown_gallery_total_uses_file_count_instead_of_bytes():
    text = card(3, None, unit="files")
    assert "<b>3</b> files downloaded" in text and "Total count" in text
    assert "MB" not in text and "3B" not in text


def test_known_total_starts_at_zero_without_question_mark():
    text = card(0, 100)
    assert "<b>0%</b>" in text and "<b>0B</b> / 100B" in text
    assert "?" not in text


@pytest.mark.parametrize(
    "stage",
    [
        captions.QUEUED,
        card(0, None),
        card(40, 100),
        card(100, None),
        captions.UPLOADING,
        captions.FAILED,
    ],
)
def test_status_stages_keep_valid_html_and_caption_budget(stage):
    info = MediaInfo(
        "yt",
        "id",
        "https://example.com/video",
        "😀<&>" * 400,
        uploader="Creator <name>" * 100,
        duration=1058,
        formats=[FormatOption("720", "720p", None)],
    )
    text = captions.status_caption(info, stage, "720")
    root = ET.fromstring("<root>" + text + "</root>")
    assert len("".join(root.itertext()).encode("utf-16-le")) // 2 <= 1024


def test_oversize_only_video_keeps_audio_choice(monkeypatch):
    info = MediaInfo(
        "yt",
        "id",
        "url",
        "Title",
        formats=[FormatOption("720", "720p", 10_000, 720), FormatOption("mp3", "MP3", 10)],
    )
    monkeypatch.setattr(media, "get_settings", lambda: SimpleNamespace(max_file_bytes=100))
    assert media.single_choice(info) is None


def test_callback_acknowledges_before_resolve_and_sends_plain_description(monkeypatch):
    events = []
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    cq = SimpleNamespace(data="desc|yt|id||", message=msg, answer=AsyncMock())
    cq.answer.side_effect = lambda **kwargs: events.append("ack")
    info = MediaInfo("yt", "id", "url", "Title", description="x" * 3998 + "<>&" * 30)

    async def resolve(*args):
        events.append("resolve")
        return info

    monkeypatch.setattr(media, "resolve_async", resolve)
    asyncio.run(callbacks.on_callback(cq, MagicMock()))
    assert events == ["ack", "resolve"]
    assert "".join(c.args[0] for c in msg.answer.call_args_list) == info.description
    assert all(c.kwargs["parse_mode"] is None for c in msg.answer.call_args_list)


def test_thumbnail_callback_uploads_local_file_and_cleans_it(monkeypatch):
    from pathlib import Path

    from aiogram.types import FSInputFile

    info = MediaInfo("yt", "id", "url", "Title", thumbnail="https://cdn.invalid/image.jpg")
    msg = MagicMock(spec=Message)
    paths = []

    async def document(file, **kwargs):
        assert isinstance(file, FSInputFile)
        paths.append(Path(file.path))
        assert paths[0].read_bytes() == b"image"

    msg.answer_document = AsyncMock(side_effect=document)
    cq = SimpleNamespace(data="thumb|yt|id||", message=msg, answer=AsyncMock())

    async def resolve(*a):
        return info

    def download(url, path, **kwargs):
        assert kwargs["max_bytes"] <= 5 * 1024 * 1024
        path.write_bytes(b"image")
        return path

    monkeypatch.setattr(media, "resolve_async", resolve)
    monkeypatch.setattr(media, "download_file", download)
    asyncio.run(callbacks.on_callback(cq, MagicMock()))
    msg.answer_document.assert_awaited_once()
    assert not paths[0].exists()


def test_callback_extraction_obeys_shared_four_slot_limit(monkeypatch):
    async def scenario():
        gate, started = asyncio.Event(), 0
        info = MediaInfo("yt", "id", "url", "Title", description="text")

        async def work(fn, *args):
            nonlocal started
            started += 1
            await gate.wait()
            return info

        monkeypatch.setattr(media, "extraction_slots", asyncio.Semaphore(4))
        monkeypatch.setattr(media.asyncio, "to_thread", work)
        queries = []
        for _ in range(8):
            msg = MagicMock(spec=Message)
            msg.answer = AsyncMock()
            queries.append(SimpleNamespace(data="desc|yt|id||", message=msg, answer=AsyncMock()))
        running = [asyncio.create_task(callbacks.on_callback(cq, MagicMock())) for cq in queries]
        await asyncio.sleep(0)
        assert started == 4
        assert all(cq.answer.await_count == 1 for cq in queries)
        gate.set()
        await asyncio.gather(*running)
        assert started == 8

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_file_mb", 0),
        ("max_source_mb", 0),
        ("max_parallel_downloads", 0),
        ("task_time_limit", 60),
        ("queue_ttl", 8 * 24 * 3600),
    ],
)
def test_invalid_runtime_limits_are_rejected(field, value):
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
