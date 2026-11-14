from pathlib import Path

import pytest

from app.models import DownloadResult
from app.platforms import registry, ytdlp
from app.platforms.sites import SoundCloud
from app.services import captions, media

SET_URL = (
    "https://soundcloud.com/soundcloud-uk/sets/jamz-best-uk-r-b"
    "?si=d2b6cb21b4ed4c7cba6af5bcbf86e465&utm_source=clipboard"
    "&utm_medium=text&utm_campaign=social_sharing"
)


def extractor(monkeypatch, result):
    class Extractor:
        def __init__(self, options):
            assert options["extract_flat"] == "in_playlist"
            assert options["noplaylist"] is False
            assert options["playlistend"] == 51

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            return result

    monkeypatch.setattr(ytdlp, "YoutubeDL", Extractor)


def playlist(count):
    return {
        "_type": "playlist",
        "title": "Jamz: Best UK R&B",
        "entries": [{"url": f"https://soundcloud.com/artist/track-{i}"} for i in range(count)],
    }


def test_soundcloud_set_preserves_all_tracks_and_tracking_link(monkeypatch):
    extractor(monkeypatch, playlist(2))
    platform, media_id, url = registry.detect(SET_URL)
    info = platform.extract(url)
    assert info.id == media_id and info.url == SET_URL
    assert info.kind == "gallery" and media.single_choice(info) == "all"
    assert media.valid_choice(info, "all", None)
    assert [(i.url, i.type) for i in info.items] == [
        ("https://soundcloud.com/artist/track-0", "audio"),
        ("https://soundcloud.com/artist/track-1", "audio"),
    ]
    assert "🎵 2 tracks" in captions.info_caption(info, 10**9)
    assert captions.info_header(info).startswith("🎵")


@pytest.mark.parametrize("count,match", [(0, "No downloadable"), (51, "up to 50")])
def test_empty_or_oversized_set_fails_without_truncation(monkeypatch, count, match):
    extractor(monkeypatch, playlist(count))
    with pytest.raises(ytdlp.UserError, match=match):
        SoundCloud().extract(SET_URL)


@pytest.mark.parametrize("entry", [None, {}, {"url": "http://localhost/private"}])
def test_unavailable_or_unrelated_track_is_not_silently_skipped(monkeypatch, entry):
    result = playlist(1)
    result["entries"].append(entry)
    extractor(monkeypatch, result)
    with pytest.raises(ytdlp.UserError, match="every track"):
        SoundCloud().extract(SET_URL)


def test_single_track_remains_audio(monkeypatch):
    extractor(monkeypatch, {"title": "Track", "formats": [{"vcodec": "none", "acodec": "opus"}]})
    info = SoundCloud().extract("https://soundcloud.com/artist/track")
    assert info.kind == "audio" and media.single_choice(info) == "mp3"


def test_set_download_converts_each_track_in_order(tmp_path, monkeypatch):
    platform = SoundCloud()
    info = platform.playlist_info(playlist(2), SET_URL, "set")
    monkeypatch.setattr(
        platform,
        "extract",
        lambda url: platform.to_media_info(
            {"title": url, "formats": [{"vcodec": "none", "acodec": "opus"}]}, url, "track"
        ),
    )
    calls, progress = [], []

    def download(source, track, fmt, lang, dest, callback):
        calls.append((source, fmt, lang))
        path = dest / "media.mp3"
        path.write_bytes(b"mp3")
        return DownloadResult(path=str(path))

    monkeypatch.setattr(platform, "download_url", download)
    result = platform.download(info, "all", None, tmp_path, lambda *args: progress.append(args))
    assert calls == [(item.url, "mp3", None) for item in info.items]
    assert [Path(path).relative_to(tmp_path).as_posix() for path, _ in result.files] == [
        "0/media.mp3",
        "1/media.mp3",
    ]
    assert all(kind == "audio" for _, kind in result.files)
    assert progress == [(0, 2), (1, 2), (2, 2)]


@pytest.mark.parametrize("failure", ["unavailable", "oversized", "download"])
def test_set_download_stops_on_failed_or_oversized_track(tmp_path, monkeypatch, failure):
    from app.config import get_settings

    platform = SoundCloud()
    info = platform.playlist_info(playlist(2), SET_URL, "set")
    track = platform.to_media_info(
        {"title": "Track", "formats": [{"vcodec": "none", "acodec": "opus"}]},
        info.items[0].url,
        "track",
    )
    if failure == "unavailable":
        track.kind = "gallery"
    monkeypatch.setattr(platform, "extract", lambda _: track)
    calls = []

    def download(source, track, fmt, lang, dest, callback):
        calls.append(source)
        if failure == "download":
            raise ytdlp.UserError("Download failed")
        path = dest / "media.mp3"
        path.write_bytes(b"too large")
        return DownloadResult(path=str(path))

    monkeypatch.setattr(platform, "download_url", download)
    monkeypatch.setattr(get_settings(), "max_file_mb", 0)
    with pytest.raises(ytdlp.UserError if failure != "oversized" else ytdlp.TooLarge):
        platform.download(info, "all", None, tmp_path, lambda *_: None)
    assert len(calls) == (0 if failure == "unavailable" else 1)
