import pytest

from app.config import get_settings
from app.models import DownloadResult, GalleryItem
from app.platforms import registry, ytdlp
from app.platforms.sites import SoundCloud
from app.services import media

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
            assert options["playlistend"] == get_settings().collection_max_items + 1

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
        "uploader": "SoundCloud UK",
        "entries": [{"url": f"https://soundcloud.com/artist/track-{i}"} for i in range(count)],
    }


def test_soundcloud_set_becomes_a_collection_of_tracks(monkeypatch):
    extractor(monkeypatch, playlist(2))
    platform, media_id, url = registry.detect(SET_URL)
    info = platform.extract(url)
    assert info.id == media_id and info.url == SET_URL
    assert info.kind == "collection" and info.extra == {"total": 2, "audio": True}
    assert info.title == "Jamz: Best UK R&B" and info.uploader == "SoundCloud UK"
    assert [(e.url, e.title) for e in info.entries] == [
        ("https://soundcloud.com/artist/track-0", "track 0"),  # titles from the link when flat
        ("https://soundcloud.com/artist/track-1", "track 1"),
    ]
    # A collection is never downloaded as one item: the batch flow takes over.
    assert media.single_choice(info) is None
    assert not media.valid_choice(info, "all", None) and not media.valid_choice(info, "mp3", None)


def test_large_set_is_truncated_to_the_limit_and_reports_the_total(monkeypatch):
    monkeypatch.setattr(get_settings(), "collection_max_items", 3)
    extractor(monkeypatch, {**playlist(4), "playlist_count": 30})
    info = SoundCloud().extract(SET_URL)
    assert len(info.entries) == 3 and info.extra["total"] == 30


def test_empty_set_fails_clearly(monkeypatch):
    extractor(monkeypatch, playlist(0))
    with pytest.raises(ytdlp.UserError, match="No downloadable"):
        SoundCloud().extract(SET_URL)


@pytest.mark.parametrize("entry", [None, {}, {"url": "http://localhost/private"}])
def test_missing_or_foreign_entries_are_left_out(monkeypatch, entry):
    result = playlist(1)
    result["entries"].append(entry)
    extractor(monkeypatch, result)
    info = SoundCloud().extract(SET_URL)
    assert [e.url for e in info.entries] == ["https://soundcloud.com/artist/track-0"]


def test_single_track_remains_audio(monkeypatch):
    extractor(monkeypatch, {"title": "Track", "formats": [{"vcodec": "none", "acodec": "opus"}]})
    info = SoundCloud().extract("https://soundcloud.com/artist/track")
    assert info.kind == "audio" and media.single_choice(info) == "mp3"


def test_set_cached_in_the_old_gallery_shape_asks_to_send_the_link_again(tmp_path):
    info = SoundCloud().to_media_info({"title": "Set"}, SET_URL, "set")
    info.kind, info.items = "gallery", [GalleryItem("https://soundcloud.com/a/b", "audio")]
    with pytest.raises(ytdlp.UserError, match="expired"):
        SoundCloud().download(info, "all", None, tmp_path, lambda *_: None)


def test_single_track_download_is_unchanged(tmp_path, monkeypatch):
    platform = SoundCloud()
    info = platform.to_media_info(
        {"title": "Track", "formats": [{"vcodec": "none", "acodec": "opus"}]},
        "https://soundcloud.com/a/b",
        "b",
    )
    calls = []
    monkeypatch.setattr(
        platform, "download_url", lambda *args: calls.append(args[0]) or DownloadResult(path="x.mp3")
    )
    assert platform.download(info, "mp3", None, tmp_path, lambda *_: None).path == "x.mp3"
    assert calls == ["https://soundcloud.com/a/b"]
