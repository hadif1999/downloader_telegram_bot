"""Collections (playlists, albums, sets): link parsing, extraction and the per-item quality preset."""

from pathlib import Path

import pytest

from app.config import get_settings
from app.models import FormatOption, MediaInfo
from app.platforms import registry, spotify, youtube, ytdlp
from app.platforms.common import url_key
from app.services import media

FIX = Path(__file__).parent / "fixtures"
LIST = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


# --- several links in one message


def test_detect_all_keeps_order_drops_repeats_and_counts_the_rest():
    text = (
        "look https://youtu.be/dQw4w9WgXcQ, and https://www.youtube.com/watch?v=dQw4w9WgXcQ "
        "plus https://example.com/page and (https://soundcloud.com/artist/track)."
    )
    found, skipped = registry.detect_all(text)
    assert [(p.name, url) for p, _, url in found] == [
        ("yt", "https://youtu.be/dQw4w9WgXcQ"),  # trailing comma is not part of the link
        ("scl", "https://soundcloud.com/artist/track"),
    ]
    assert skipped == 1  # example.com


def test_detect_all_caps_links_and_counts_the_extra_ones():
    text = " ".join(f"https://soundcloud.com/a/t{i}" for i in range(5))
    found, skipped = registry.detect_all(text, 3)
    assert len(found) == 3 and skipped == 2
    assert registry.detect(text)[2] == "https://soundcloud.com/a/t0"


# --- YouTube playlists


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/playlist?list={LIST}",
        f"https://youtube.com/playlist?si=x&list={LIST}",
        f"https://music.youtube.com/playlist?list={LIST}",
    ],
)
def test_youtube_playlist_links_get_a_short_stable_id(url):
    platform, media_id, _ = registry.detect(url)
    assert platform.name == "yt"
    assert media_id == url_key(youtube.playlist_url(LIST)) and len(media_id) <= 22
    assert platform.url_for(media_id) is None  # resolved from the stored source link


def test_watch_link_inside_a_playlist_stays_a_single_video():
    _, media_id, _ = registry.detect(f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={LIST}")
    assert media_id == "dQw4w9WgXcQ"


def flat_playlist(entries, **extra):
    return {"_type": "playlist", "title": "Lo-fi beats", "channel": "Chill", "entries": entries, **extra}


@pytest.fixture
def yt_listing(monkeypatch):
    seen = {}

    def install(result):
        class Extractor:
            def __init__(self, options):
                seen["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download=False):
                seen["url"] = url
                return result

        monkeypatch.setattr(youtube, "YoutubeDL", Extractor)
        return seen

    return install


def test_youtube_playlist_lists_entries_and_skips_private_ones(yt_listing):
    seen = yt_listing(
        flat_playlist(
            [
                {"id": "aaaaaaaaaaa", "title": "One", "duration": 61, "channel": "A"},
                {"id": "bbbbbbbbbbb", "title": "[Private video]"},
                {"id": "not-an-id", "title": "Broken"},
                None,
                {"id": "ccccccccccc", "title": "Two"},
            ],
            playlist_count=5,
        )
    )
    url = f"https://www.youtube.com/playlist?list={LIST}"
    info = youtube.YouTube().extract(url)
    assert seen["url"] == youtube.playlist_url(LIST)
    assert seen["options"]["extract_flat"] == "in_playlist" and seen["options"]["noplaylist"] is False
    assert info.kind == "collection" and info.title == "Lo-fi beats" and info.uploader == "Chill"
    assert [(e.url, e.title, e.duration) for e in info.entries] == [
        ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "One", 61),
        ("https://www.youtube.com/watch?v=ccccccccccc", "Two", None),
    ]
    assert info.extra == {"total": 5, "audio": False}


def test_youtube_music_album_is_audio(yt_listing):
    yt_listing(flat_playlist([{"id": "aaaaaaaaaaa", "title": "Song"}]))
    info = youtube.YouTube().extract("https://music.youtube.com/playlist?list=OLAK5uy_abcdefghijklmnop")
    assert info.extra["audio"] is True


def test_cached_collection_round_trips():
    info = MediaInfo("yt", "k", "u", "T", kind="collection", extra={"total": 1, "audio": False})
    info.entries = [ytdlp.CollectionEntry("https://youtu.be/aaaaaaaaaaa", "One", 3, "A")]
    assert MediaInfo.from_dict(info.to_dict()) == info


# --- Spotify albums and playlists


def test_spotify_embed_track_list():
    entity = spotify.parse_embed((FIX / "spotify_album_embed.html").read_text())
    entries = spotify.embed_entries(entity)
    assert entity["title"] == "Global Warming"
    assert entries[0].url == "https://open.spotify.com/track/6OmhkSOpvYBokMKQxpIGx2"
    assert entries[0].title == "Global Warming (feat. Sensato)"
    assert entries[0].uploader == "Pitbull, Sensato" and entries[0].duration == 85
    assert spotify.embed_cover(entity).startswith("https://")


def test_spotify_album_link_becomes_a_collection(monkeypatch):
    pages = []

    def fetch(url, headers=None, timeout=20):
        pages.append(url)
        return url, (FIX / "spotify_album_embed.html").read_text()

    monkeypatch.setattr(spotify, "fetch", fetch)
    url = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy?si=x"
    platform, media_id, _ = registry.detect(url)
    info = platform.extract(url)
    assert pages == ["https://open.spotify.com/embed/album/4aawyAB9vmqN3uQ7FjRGTy"]
    assert media_id == "4aawyAB9vmqN3uQ7FjRGTy" and info.kind == "collection"
    assert info.extra["audio"] and len(info.entries) == 3 and info.uploader == "Pitbull"
    assert all(registry.match(e.url)[0].name == "sp" for e in info.entries)


@pytest.mark.parametrize(
    "page", ["<html></html>", '<script id="__NEXT_DATA__" type="application/json">{}</script>']
)
def test_unreadable_spotify_embed_fails_clearly(page):
    with pytest.raises(ytdlp.UserError, match="Spotify album or playlist"):
        spotify.parse_embed(page)


def test_spotify_artist_links_are_refused():
    with pytest.raises(ytdlp.UserError, match="tracks, albums and playlists"):
        spotify.Spotify().extract("https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg")


# --- quality preset per item

MB = 1 << 20


def video(*heights, sizes=None):
    sizes = sizes or {}
    formats = [FormatOption(str(h), f"{h}p", sizes.get(h, MB), h) for h in heights]
    return MediaInfo("yt", "id", "u", "T", formats=[*formats, FormatOption("mp3", "MP3", MB)])


@pytest.mark.parametrize(
    "preset,heights,expected",
    [
        ("720", (1080, 720, 360), "720"),
        ("1080", (720, 360), "720"),
        ("480", (1080, 720), "720"),  # nothing low enough: the smallest available
        ("mp3", (1080,), "mp3"),
    ],
)
def test_preset_picks_the_best_format_under_the_cap(preset, heights, expected):
    assert media.preset_choice(video(*heights), preset) == expected


def test_preset_skips_formats_over_the_upload_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_file_mb", 10)
    assert media.preset_choice(video(1080, 720, sizes={1080: 50 * MB}), "1080") == "720"
    assert media.preset_choice(video(1080, sizes={1080: 50 * MB}), "1080") is None


def test_preset_keeps_audio_as_mp3_and_posts_whole():
    audio = MediaInfo("scl", "id", "u", "T", kind="audio", formats=[FormatOption("mp3", "MP3", None)])
    post = MediaInfo("ig", "id", "u", "T", kind="gallery")
    assert media.preset_choice(audio, "1080") == "mp3"
    assert media.preset_choice(post, "mp3") == "all"
