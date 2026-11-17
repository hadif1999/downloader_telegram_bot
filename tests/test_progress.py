import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from yt_dlp import YoutubeDL as BaseYoutubeDL

from app.bot.handlers import callbacks
from app.models import FormatOption, MediaInfo
from app.platforms import ytdlp
from app.services import captions, keyboards, requests
from app.services.progress import Cancelled, Monitor, clock

MB = 1 << 20


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_running_download_shows_bar_size_part_speed_eta_and_steps():
    now = Clock()
    m = Monitor(now=now)
    m(0, 400 * MB, track="video", part=1, parts=2, part_done=0, part_size=200 * MB)
    now.t += 10
    m(250 * MB, 400 * MB, track="video", part=2, parts=2, part_done=50 * MB, part_size=200 * MB)
    text = m.render()
    assert "⬇️ <b>Downloading</b> · video track" in text
    assert "<code>▓▓▓▓▓▓░░░░</code> <b>62%</b>" in text
    assert "📦 <b>250.0MB</b> / 400.0MB" in text
    assert "🧩 Part <b>2</b> of 2 <code>▓░░░░░</code> 25%" in text
    assert "🚀 25.0MB/s · ⏳ 6s left" in text
    assert "⏱ Elapsed 10s" in text
    assert "<i>✅ Queue › 🔵 Download › ⚪ Process › ⚪ Upload</i>" in text


def test_single_part_and_unknown_size_hide_what_is_not_known():
    m = Monitor(now=Clock())
    m(5 * MB, 8 * MB, part=1, parts=1, part_done=5 * MB, part_size=8 * MB)
    assert "Part" not in m.render()  # one part is not worth a line
    m(3 * MB, None)
    text = m.render()
    assert "<b>3.0MB</b> received" in text and "Total size not provided" in text
    assert "measuring speed" in text and "left" not in text and "%" not in text


def test_segments_without_sizes_show_a_count_only():
    m = Monitor(now=Clock())
    m(MB, 10 * MB, part=12, parts=340)
    assert "🧩 Part <b>12</b> of 340\n" in m.render()


def test_gallery_counts_files():
    m = Monitor("files", now=Clock())
    m(2, 5)
    text = m.render()
    assert "🗂 <b>2</b> / 5 files" in text and "Fetching file 3 of 5" in text and "40%" in text


@pytest.mark.parametrize(
    ("stage", "title", "step"),
    [
        ("merge", "Merging video and audio", "🔵 Process"),
        ("convert", "Converting to MP3", "🔵 Process"),
        ("finalize", "Adding tags and cover art", "🔵 Process"),
    ],
)
def test_processing_steps_after_the_download(stage, title, step):
    now = Clock()
    m = Monitor(now=now)
    m(10, 10)
    m(10, 10, stage=stage)
    now.t += 65
    text = m.render()
    assert f"<b>{title}</b>" in text and "Step time 1m 05s" in text
    assert f"✅ Download › {step}" in text


def test_upload_step_shows_final_size_and_removes_cancel():
    m = Monitor(now=Clock())
    assert m.cancellable
    m.set_stage("upload", size=275 * MB)
    text = m.render()
    assert text.startswith("📤 <b>Uploading to Telegram</b>") and "📦 275.0MB" in text
    assert "✅ Process › 🔵 Upload" in text
    assert not m.cancellable


def test_cancel_flag_stops_the_download_thread_on_its_next_report():
    m = Monitor(now=Clock())
    m(1, 10)
    m.cancel.set()
    with pytest.raises(Cancelled):
        m(2, 10)
    assert "Cancelling" in m.render() and not m.cancellable


def test_speed_restarts_after_a_retry_rewinds_the_counter():
    now = Clock()
    m = Monitor(now=now)
    m(0, 100 * MB)
    now.t += 5
    m(50 * MB, 100 * MB)
    now.t += 1
    m(10 * MB, 100 * MB)  # part retried from its start
    assert m.speed() is None


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "—"), (9, "9s"), (185, "3m 05s"), (3720, "1h 02m"), (float("inf"), "—")]
)
def test_clock(seconds, text):
    assert clock(seconds) == text


# --------------------------------------------------------------- yt-dlp hooks


def test_ytdlp_progress_reports_track_and_part_of_each_stream():
    events = []
    tracker = ytdlp.Progress(lambda *a, **d: events.append((a, d)), 600, part_bytes=200)
    video = {"vcodec": "avc1", "acodec": "none"}
    tracker({"status": "downloading", "downloaded_bytes": 250, "total_bytes": 500, "info_dict": video})
    tracker({"status": "finished", "filename": "v.mp4", "downloaded_bytes": 500})
    audio = {"vcodec": "none", "acodec": "mp4a"}
    tracker({"status": "downloading", "downloaded_bytes": 40, "total_bytes": 100, "info_dict": audio})
    assert events[0] == (
        (250, 600),
        {"track": "video", "part": 2, "parts": 3, "part_done": 50, "part_size": 200},
    )
    assert events[1] == (
        (540, 600),
        {"track": "audio", "part": 1, "parts": 1, "part_done": 40, "part_size": 100},
    )


def test_ytdlp_progress_uses_hls_segments_as_parts():
    events = []
    tracker = ytdlp.Progress(lambda *a, **d: events.append(d), None, part_bytes=200)
    tracker({"status": "downloading", "downloaded_bytes": 5, "fragment_index": 11, "fragment_count": 340})
    assert events == [{"part": 12, "parts": 340}]


def test_ytdlp_postprocessors_become_stages():
    events = []
    tracker = ytdlp.Progress(lambda *a, **d: events.append(d.get("stage")), None)
    for name in ("Merger", "MoveFiles", "ExtractAudio", "Metadata"):
        tracker.postprocessed({"status": "started", "postprocessor": name})
        tracker.postprocessed({"status": "finished", "postprocessor": name})
    assert events == ["merge", "convert", "finalize"]


def test_cancel_raised_in_a_hook_aborts_a_real_ytdlp_download(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (4 * MB))

    def hook(d):
        raise Cancelled

    opts = {"quiet": True, "enable_file_urls": True, "outtmpl": str(tmp_path / "out.%(ext)s")}
    with BaseYoutubeDL(opts | {"progress_hooks": [hook]}) as ydl, pytest.raises(Cancelled):
        ydl.extract_info(source.as_uri(), download=True)


# --------------------------------------------------------------- request store


def test_cancel_outcomes_follow_the_request_phase(request_store):
    assert requests.create("q", 10, "yt|id|720|")
    assert requests.request_cancel("q", 99) == requests.CANCEL_UNKNOWN  # someone else's button
    assert requests.request_cancel("q", 10) == requests.CANCEL_STOPPED
    assert requests.load("q", 10)["phase"] == "cancelled"
    assert request_store.get(requests.keys("q", 10)[2]) is None  # user may start a new download
    assert requests.claim("q", 10, "w") == -1  # the worker skips it

    assert requests.create("r", 10)
    assert requests.claim("r", 10, "w") == 1
    assert not requests.cancel_requested("r", 10)
    assert requests.request_cancel("r", 10) == requests.CANCEL_SIGNALLED
    assert requests.cancel_requested("r", 10)
    requests.transition("r", 10, "w", "sending")
    assert requests.request_cancel("r", 10) == requests.CANCEL_TOO_LATE


# --------------------------------------------------------------- bot button


def callback(user_id, data, photo=False):
    msg = MagicMock(spec=Message)
    msg.photo = [object()] if photo else None
    msg.edit_text = AsyncMock()
    msg.edit_caption = AsyncMock()
    return NS(data=data, from_user=NS(id=user_id), message=msg, answer=AsyncMock())


INFO = MediaInfo(
    "yt", "id", "https://youtu.be/abcdefghijk", "Title", formats=[FormatOption("720", "720p", 1, 720)]
)


def test_cancel_button_on_queued_job_redraws_card_with_format_menu(request_store, monkeypatch):
    monkeypatch.setattr(callbacks.media, "resolve_async", AsyncMock(return_value=INFO))
    assert requests.create("q", 10, "yt|id|720|")
    cq = callback(10, keyboards.cancel_kb("q").inline_keyboard[0][0].callback_data)
    asyncio.run(callbacks.on_cancel(cq))
    assert cq.answer.await_args.args[0] == captions.CANCEL_DONE
    text = cq.message.edit_text.await_args.args[0]
    assert text.startswith(captions.CANCELLED) and "Title" in text
    assert cq.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard  # pick again


def test_cancel_button_of_running_job_signals_worker(request_store):
    assert requests.create("r", 10)
    requests.claim("r", 10, "w")
    cq = callback(10, "stop|r")
    asyncio.run(callbacks.on_cancel(cq))
    assert cq.answer.await_args.args[0] == captions.CANCEL_PENDING
    assert requests.cancel_requested("r", 10)
    cq.message.edit_text.assert_not_awaited()  # the worker redraws its own card


def test_other_users_cannot_cancel(request_store):
    assert requests.create("q", 10)
    cq = callback(11, "stop|q")
    asyncio.run(callbacks.on_cancel(cq))
    assert cq.answer.await_args.kwargs["show_alert"] is True
    assert requests.load("q", 10)["phase"] == "queued"
