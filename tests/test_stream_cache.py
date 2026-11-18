"""The worker reuses the bot's extraction (format URLs) instead of looking the link up again."""

import subprocess
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL as BaseYoutubeDL

from app.models import FormatOption, MediaInfo
from app.platforms import ytdlp


class CountingYDL(BaseYoutubeDL):
    """The real yt-dlp (local file URLs only), counting fresh extractions."""

    extractions = 0

    def extract_info(self, *args, **kwargs):
        type(self).extractions += 1
        return super().extract_info(*args, **kwargs)


@pytest.fixture
def local_video(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    return source


@pytest.fixture
def platform(monkeypatch):
    CountingYDL.extractions = 0
    monkeypatch.setattr(ytdlp, "YoutubeDL", CountingYDL)
    p = ytdlp.YtDlpPlatform()
    monkeypatch.setattr(p, "ydl_opts", lambda: {"quiet": True, "no_warnings": True, "enable_file_urls": True})
    return p


def media(url: str) -> MediaInfo:
    return MediaInfo("generic", "local", url, "Title", formats=[FormatOption("best", "Video", None)])


def extracted(url: str) -> dict:
    with BaseYoutubeDL({"quiet": True, "enable_file_urls": True}) as ydl:
        return ydl.extract_info(url, download=False)


def test_download_uses_cached_streams_without_a_second_lookup(request_store, platform, local_video, tmp_path):
    url = local_video.as_uri()
    ytdlp.save_streams("generic", "local", extracted(url))
    out = tmp_path / "out"
    out.mkdir()
    result = platform.download_url(url, media(url), "best", None, out, lambda *a, **k: None)
    assert Path(result.path).is_file()
    assert CountingYDL.extractions == 0  # straight to the cached format URLs


def test_expired_cached_urls_fall_back_to_a_fresh_lookup(
    request_store, platform, local_video, tmp_path, monkeypatch
):
    monkeypatch.setattr(ytdlp, "retry_delay", lambda attempt: 0)  # no back-off between attempts
    url = local_video.as_uri()
    stale = extracted(url)
    for f in stale["formats"]:
        f["url"] = (tmp_path / "gone.mp4").as_uri()  # like an expired googlevideo URL
    stale["url"] = (tmp_path / "gone.mp4").as_uri()
    ytdlp.save_streams("generic", "local", stale)
    out = tmp_path / "out"
    out.mkdir()
    result = platform.download_url(url, media(url), "best", None, out, lambda *a, **k: None)
    assert Path(result.path).is_file() and CountingYDL.extractions == 1
    assert ytdlp.load_streams("generic", "local") is None  # the stale entry is gone


def test_cache_is_used_only_for_the_media_it_was_extracted_from(
    request_store, platform, local_video, tmp_path
):
    url = local_video.as_uri()
    ytdlp.save_streams("generic", "local", extracted(url))
    out = tmp_path / "out"
    out.mkdir()
    # e.g. Spotify downloads a YouTube source for its own media: never the cached entry
    platform.download_url(
        url, media("https://open.spotify.com/track/x"), "best", None, out, lambda *a, **k: None
    )
    assert CountingYDL.extractions == 1


def test_cached_entry_is_trimmed_and_expires(request_store):
    info = {
        "id": "x",
        "title": "T",
        "automatic_captions": {"en": [{"url": "u" * 50_000}]},
        "heatmap": [{"start_time": 1}] * 1000,
        "formats": [
            {"format_id": "sb0", "protocol": "mhtml", "url": "https://storyboard"},
            {"format_id": "18", "protocol": "https", "url": "https://video"},
        ],
    }
    ytdlp.save_streams("yt", "x", info)
    cached = ytdlp.load_streams("yt", "x")
    assert "automatic_captions" not in cached and "heatmap" not in cached
    assert [f["format_id"] for f in cached["formats"]] == ["18"]
    ttl = request_store.ttl(next(iter(request_store.scan_iter("streams:*"))))
    assert 0 < ttl <= 2 * 3600  # INFO_CACHE_TTL default, below the 4 h URL safety cap


def test_cache_failures_never_break_lookups_or_downloads(monkeypatch):
    from app.services import cache

    monkeypatch.setattr(cache, "client", lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    ytdlp.save_streams("yt", "x", {"formats": []})
    assert ytdlp.load_streams("yt", "x") is None
    ytdlp.drop_streams("yt", "x")


def test_link_cache_lasts_two_hours_by_default(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("INFO_CACHE_TTL", raising=False)
    assert Settings(bot_token="1:x").info_cache_ttl == 7200
