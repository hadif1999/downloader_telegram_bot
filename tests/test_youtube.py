import json
from pathlib import Path

import pytest

from app.platforms import registry
from app.platforms.youtube import YouTube, format_selector, to_media_info

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "yt_multiaudio.json").read_text())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=0MNVhXEX9to", "0MNVhXEX9to"),
        ("https://youtube.com/watch?feature=share&v=0MNVhXEX9to&t=3", "0MNVhXEX9to"),
        ("https://youtu.be/0MNVhXEX9to?si=abc", "0MNVhXEX9to"),
        ("https://m.youtube.com/shorts/0MNVhXEX9to", "0MNVhXEX9to"),
        ("https://music.youtube.com/watch?v=0MNVhXEX9to", "0MNVhXEX9to"),
        ("https://www.youtube.com/live/0MNVhXEX9to", "0MNVhXEX9to"),
        ("https://example.com/watch?v=0MNVhXEX9to", None),
    ],
)
def test_match(url, expected):
    assert YouTube().match(url) == expected


def test_detect_in_text_and_stubs():
    p, media_id, _ = registry.detect("check this out https://youtu.be/0MNVhXEX9to !!")
    assert (p.name, media_id) == ("yt", "0MNVhXEX9to")
    p, _, _ = registry.detect("https://www.instagram.com/reel/abc123/")
    assert p.name == "ig" and not p.implemented
    assert registry.detect("hello there") is None


def test_media_info_matches_reference():
    info = to_media_info(FIXTURE)
    assert info.title.startswith("Reinforcement Learning")
    assert info.uploader_handle == "@Eigensteve"
    assert info.upload_date == "2021-02-12"
    assert info.category == "Science & Technology"
    keys = [f.key for f in info.formats]
    assert keys[-1] == "mp3"
    assert [k for k in keys if k.isdigit()][-6:] == ["1080", "720", "480", "360", "240", "144"]
    mib = {f.key: round(f.size / 2**20, 1) for f in info.formats}
    assert mib["1080"] == 125.6 and mib["mp3"] == 24.1  # same numbers as the reference screenshot


def test_audio_tracks():
    tracks = to_media_info(FIXTURE).audio_tracks
    assert tracks[0].original and tracks[0].lang == "en-US"
    assert {"DE-DE", "ES-US", "FR-FR", "HI", "ID", "IT", "JA", "PL", "PT-BR"} <= {t.label for t in tracks}


def test_single_audio_has_no_tracks():
    single = {**FIXTURE, "formats": [f for f in FIXTURE["formats"] if f.get("language") in (None, "en-US")]}
    assert to_media_info(single).audio_tracks == []


def test_format_selector():
    assert format_selector(1080, None).startswith("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/")
    assert "[language=de-DE]" in format_selector(720, "de-DE")
    assert format_selector(None, None).startswith("ba[ext=m4a]")


def test_best_thumbnail_skips_missing():
    from app.platforms.youtube import best_thumbnail

    info = {
        "id": "abc",
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg", "preference": 0, "width": 1280},
            {"url": "https://i.ytimg.com/vi_webp/abc/maxresdefault.webp", "preference": 1},
            {"url": "https://i.ytimg.com/vi/abc/sddefault.jpg", "preference": -1, "width": 640},
        ],
    }
    assert best_thumbnail(info, check=lambda u: "/sddefault" in u).endswith("sddefault.jpg")
    assert best_thumbnail(info, check=lambda u: False).endswith("abc/hqdefault.jpg")
