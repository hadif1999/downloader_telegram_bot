import json
from pathlib import Path

import pytest

from app.models import GalleryItem, MediaInfo
from app.platforms import gallery, registry
from app.platforms.common import clean_url, og_tags, url_key
from app.platforms.spotify import parse_track, pick_match
from app.platforms.threads import find_post, post_to_media_info
from app.platforms.ytdlp import build_formats, format_selector
from app.services import captions
from app.services.keyboards import formats_kb, parse_cb
from app.services.media import single_choice

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "url,platform",
    [
        ("https://www.instagram.com/reel/C9e3x0aSEP1/?igsh=abc", "ig"),
        ("https://instagram.com/p/aye83DjauH/", "ig"),
        ("https://www.instagram.com/stories/someone/3456789/", "ig"),
        ("https://vm.tiktok.com/ZMabc123/", "tt"),
        ("https://www.tiktok.com/@user/video/6742501081818877190", "tt"),
        ("https://www.tiktok.com/@user/photo/7300000000000000000", "tt"),
        ("https://pin.it/1AbCdEf", "pin"),
        ("https://www.pinterest.co.uk/pin/664281013778109217/", "pin"),
        ("https://www.snapchat.com/spotlight/W7_EDlXWTBi", "sc"),
        ("https://likee.video/@user/video/7093444807096327263", "lk"),
        ("https://vk.com/video-77521_162222515", "vk"),
        ("https://vkvideo.ru/clip-1_2", "vk"),
        ("https://www.facebook.com/watch/?v=10153231379946729", "fb"),
        ("https://fb.watch/abcDEF/", "fb"),
        ("https://www.threads.net/@nasa/post/Dfc_lfaAgnE", "th"),
        ("https://www.threads.com/@nasa/post/Dfc_lfaAgnE?xmt=1", "th"),
        ("https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy", "scl"),
        ("https://on.soundcloud.com/abc", "scl"),
        ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=x", "sp"),
        ("https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT", "sp"),
        ("https://spotify.link/AbCdEf", "sp"),
        ("instagram.com/p/aye83DjauH/", "ig"),  # without scheme
    ],
)
def test_detect_platforms(url, platform):
    p, media_id, _ = registry.detect(f"look: {url} nice")
    assert p.name == platform
    assert media_id and len(f"fmt|{p.name}|{media_id}|1080|pt-BR".encode()) <= 64


def test_unrelated_links_ignored():
    assert registry.detect("https://example.com/video.mp4 and https://github.com/x") is None


def test_url_key_ignores_tracking():
    a = url_key("https://www.instagram.com/reel/ABC/?igsh=xyz&utm_source=ig_web")
    b = url_key("https://instagram.com/reel/ABC")
    assert a == b and len(a) == 12
    # meaningful params are kept
    assert clean_url("https://www.facebook.com/watch/?v=123&fbclid=zz") == "https://facebook.com/watch?v=123"


def test_og_tags():
    tags = og_tags(
        '<meta property="og:title" content="A &amp; B"/><meta name="music:duration" content="214">'
    )
    assert tags == {"og:title": "A & B", "music:duration": "214"}


def test_spotify_track():
    meta = parse_track(og_tags((FIX / "spotify_track.html").read_text()))
    assert meta["title"] == "Never Gonna Give You Up"
    assert meta["artist"] == "Rick Astley"
    assert meta["album"] == "Whenever You Need Somebody"
    assert (meta["year"], meta["duration"]) == ("1987", 214)
    assert meta["cover"].startswith("https://i.scdn.co/")


def test_spotify_pick_match_by_duration():
    entries = [
        {"url": "a", "duration": 300},  # live version
        {"url": "b", "duration": 212},
        {"url": "c", "duration": 180},
    ]
    assert pick_match(entries, 214)["url"] == "b"
    assert pick_match(entries, None)["url"] == "a"
    assert pick_match([{"url": "x", "duration": 999}], 214)["url"] == "x"  # nothing close: top result
    assert pick_match([], 214) is None


def test_threads_video_post():
    page = (FIX / "threads_page.html").read_text()
    post = find_post(page, "Dfc_lfaAgnE")
    info = post_to_media_info(post, "https://www.threads.com/@nasa/post/Dfc_lfaAgnE")
    assert info.kind == "video" and info.extra["direct_url"].startswith("https://")
    assert info.uploader_handle == "@nasa" and info.upload_date
    assert [f.key for f in info.formats] == ["best", "mp3"]
    assert single_choice(info) == "best"


def test_threads_carousel_post():
    page = (FIX / "threads_page.html").read_text()
    info = post_to_media_info(find_post(page, "DfihFPxkaZ1"), "u")
    assert info.kind == "gallery" and [i.type for i in info.items] == ["photo", "photo"]
    assert single_choice(info) == "all"
    assert find_post(page, "missing") is None


def test_gallery_dl_dump_parsing():
    dump = json.dumps(
        [
            [2, {"username": "bob", "description": "hello\nworld", "date": "2024-01-02 10:00:00"}],
            [3, "https://cdn/x/1.jpg?sig=1", {"extension": "jpg"}],
            [3, "https://cdn/x/2.mp4", {"extension": "mp4"}],
            [3, "ytdl:https://www.tiktok.com/@a/video/1", {"extension": "mp4"}],
        ]
    )
    meta, items = gallery.parse_dump(dump)
    assert [i.type for i in items] == ["photo", "video", "video"]
    info = gallery.to_media_info("ig", "k", "u", "Instagram", meta, items)
    assert (info.title, info.uploader, info.upload_date) == ("hello", "bob", "2024-01-02")
    assert info.thumbnail == "https://cdn/x/1.jpg?sig=1"
    with pytest.raises(RuntimeError):
        gallery.parse_dump(json.dumps([[-1, {"error": "AuthRequired", "message": "login required"}]]))


def test_build_formats_progressive_single_quality():
    # TikTok/Instagram style: several progressive (video+audio) variants of one resolution
    info = {
        "duration": 10,
        "formats": [
            {"vcodec": "h264", "acodec": "aac", "width": 576, "height": 1024, "filesize": 2_000_000},
            {"vcodec": "h265", "acodec": "aac", "width": 576, "height": 1024, "filesize": 1_500_000},
        ],
    }
    kind, fmts = build_formats(info)
    assert kind == "video" and [(f.key, f.size) for f in fmts] == [
        ("480", 2_000_000),
        ("mp3", 10 * 24_000 + 65_536),
    ]  # nearest standard size


def test_build_formats_audio_only_and_unknown_resolution():
    kind, fmts = build_formats({"formats": [{"vcodec": "none", "acodec": "opus", "ext": "opus", "tbr": 64}]})
    assert kind == "audio" and [f.key for f in fmts] == ["mp3"]
    kind, fmts = build_formats({"url": "https://x/v.mp4", "ext": "mp4"})  # direct file, no metadata
    assert kind == "video" and [f.key for f in fmts] == ["best", "mp3"]


def test_hls_size_estimated_from_bitrate():
    info = {
        "duration": 100,
        "formats": [{"vcodec": "avc1", "acodec": "mp4a", "height": 720, "width": 1280, "tbr": 800}],
    }
    _, fmts = build_formats(info)
    assert fmts[0].size == 800 * 125 * 100


def test_format_selector_best_and_audio_fallbacks():
    assert format_selector(None).endswith("/b")
    assert "[height<=" not in format_selector(None)
    assert format_selector(None, audio=True).endswith("/ba/b")  # progressive-only sources


def test_single_choice_rules():
    from app.platforms.youtube import to_media_info

    yt = to_media_info(json.loads((FIX / "yt_multiaudio.json").read_text()))
    assert single_choice(yt) is None  # many resolutions + dubbed audio: show the card
    audio = MediaInfo("sp", "id", "u", "t", kind="audio", formats=[])
    assert single_choice(audio) == "mp3"


def test_gallery_card():
    info = MediaInfo(
        "ig",
        "k",
        "u",
        "t",
        kind="gallery",
        items=[GalleryItem("a"), GalleryItem("b", "video")],
        thumbnail="a",
    )
    text = captions.info_caption(info, 10**9)
    assert "🖼 1 photo · 🎬 1 video" in text and text.startswith("🖼")
    rows = formats_kb(info, 10**9).inline_keyboard
    assert rows[0][0].text == "📥 Download (2)" and parse_cb(rows[0][0].callback_data)[3] == "all"
    assert [b.text for b in rows[-1]] == ["🖼"]  # no description -> no 📝 button
