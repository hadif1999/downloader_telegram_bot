import array
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL as BaseYoutubeDL
from yt_dlp.utils import ExtractorError

from app.models import FormatOption, MediaInfo
from app.platforms import gallery, ytdlp


def select(formats, height, lang=None):
    opts = {
        "quiet": True,
        "check_formats": False,
        "format": ytdlp.format_selector(height, lang),
        "allow_multiple_audio_streams": bool(lang),
    }
    with BaseYoutubeDL(opts) as ydl:
        return ydl.process_ie_result(
            {"id": "local", "title": "Local", "extractor": "test", "formats": formats}, download=False
        )


def test_resolution_never_falls_back_above_requested_height():
    formats = [
        {
            "format_id": "1080",
            "url": "https://media.invalid/a.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "height": 1080,
            "width": 1920,
        }
    ]
    with pytest.raises(ExtractorError):
        select(formats, 720)


def test_missing_requested_audio_never_silently_uses_another_language():
    formats = [
        {
            "format_id": "720",
            "url": "https://media.invalid/a.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "height": 720,
            "width": 1280,
            "language": "en",
        }
    ]
    with pytest.raises(ExtractorError):
        select(formats, 720, "de")


def test_multi_item_post_never_silently_collapses_to_first(monkeypatch):
    p = ytdlp.YtDlpPlatform()
    p.domains, p.patterns, p.gallery = ("example.com",), (r"example\.com/",), True
    p.__init__()
    result = {
        "_type": "playlist",
        "entries": [{"url": "https://cdn.invalid/one.mp4"}, {"url": "https://cdn.invalid/two.mp4"}],
    }

    class Extractor:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            return result

    monkeypatch.setattr(ytdlp, "YoutubeDL", Extractor)
    monkeypatch.setattr(p, "_gallery", lambda *a: None)
    with pytest.raises(ytdlp.UserError, match="every item"):
        p.extract("https://example.com/post")


def ffmpeg(*args):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for local media regression checks")
    return subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *map(str, args)], capture_output=True, check=True, timeout=20
    )


def test_animated_gallery_image_converts_one_frame(tmp_path):
    source = tmp_path / "animated.gif"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=16x16:r=1", "-frames:v", "2", source)
    result = gallery._to_jpeg(source)
    assert result.suffix == ".jpg" and result.is_file() and not source.exists()


@pytest.mark.parametrize("fmt", ["best", "mp3"])
def test_local_video_and_mp3_outputs_exist_after_postprocessing(tmp_path, monkeypatch, fmt):
    source = tmp_path / "source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "0.5",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        source,
    )
    # Local fixture URLs are enabled only in this test's unguarded base downloader.
    monkeypatch.setattr(ytdlp, "YoutubeDL", BaseYoutubeDL)
    p = ytdlp.YtDlpPlatform()
    monkeypatch.setattr(p, "ydl_opts", lambda: {"quiet": True, "no_warnings": True, "enable_file_urls": True})
    info = MediaInfo(
        "generic",
        "local",
        source.as_uri(),
        "Title",
        formats=[FormatOption("best", "Video", None), FormatOption("mp3", "MP3", None)],
    )
    dest = tmp_path / "out"
    dest.mkdir()
    result = p.download_url(source.as_uri(), info, fmt, None, dest, lambda *a: None)
    assert Path(result.path).is_file()
    assert Path(result.path).suffix == (".mp3" if fmt == "mp3" else ".mp4")


def test_progressive_video_replaces_embedded_audio_with_requested_track(tmp_path, monkeypatch):
    video, dubbed = tmp_path / "english.mp4", tmp_path / "german.m4a"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        video,
    )
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-t", "1", "-c:a", "aac", dubbed)
    raw = {
        "id": "local",
        "title": "Local",
        "extractor": "test",
        "formats": [
            {
                "format_id": "video-en",
                "url": video.as_uri(),
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
                "height": 720,
                "width": 1280,
                "language": "en",
            },
            {
                "format_id": "audio-de",
                "url": dubbed.as_uri(),
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "language": "de",
                "abr": 128,
            },
        ],
    }

    class FixtureDL(BaseYoutubeDL):
        def extract_info(self, source, download=True, **kwargs):
            return self.process_ie_result(copy.deepcopy(raw), download=download)

    monkeypatch.setattr(ytdlp, "YoutubeDL", FixtureDL)
    p = ytdlp.YtDlpPlatform()
    monkeypatch.setattr(p, "ydl_opts", lambda: {"quiet": True, "no_warnings": True, "enable_file_urls": True})
    info = MediaInfo(
        "generic",
        "local",
        "https://media.invalid/post",
        "Title",
        formats=[FormatOption("720", "720p", None, 720)],
    )
    dest = tmp_path / "out"
    dest.mkdir()
    result = p.download_url(info.url, info, "720", "de", dest, lambda *a: None)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            result.path,
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert len(json.loads(probe.stdout)["streams"]) == 1
    pcm = ffmpeg(
        "-ss", "0.1", "-i", result.path, "-t", "0.5", "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1"
    ).stdout
    samples = array.array("h", pcm)
    crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:], strict=False))
    frequency = crossings * 8000 / (2 * len(samples))
    assert 800 < frequency < 960  # chosen 880 Hz track, not original 440 Hz


def test_mp3_estimate_uses_encoded_bitrate_not_input_codec():
    _, formats = ytdlp.build_formats(
        {
            "duration": 5,
            "formats": [{"vcodec": "none", "acodec": "flac", "ext": "flac", "filesize": 10_000_000}],
        }
    )
    assert formats[0].size == 5 * 24_000 + 65_536
