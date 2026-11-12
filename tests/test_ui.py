import json
from pathlib import Path

from app.models import MediaInfo
from app.platforms.registry import PLATFORMS
from app.platforms.youtube import to_media_info
from app.services import captions
from app.services.keyboards import formats_kb, languages_kb, parse_cb, result_kb

INFO = to_media_info(json.loads((Path(__file__).parent / "fixtures" / "yt_multiaudio.json").read_text()))
MAX = 2000 * 2**20


def test_roundtrip_cache_dict():
    assert MediaInfo.from_dict(json.loads(json.dumps(INFO.to_dict()))) == INFO


def test_caption_contents():
    text = captions.info_caption(INFO, MAX)
    assert "👤 Steve Brunton" in text and "@Eigensteve" in text
    assert "⏱ 26:02" in text or "⏱ 26:03" in text
    assert "✅ 1080p - 125.6MB" in text and "✅ MP3 - 24.1MB" in text
    assert len(text) <= captions.CAPTION_LIMIT


def test_oversize_formats_marked_and_hidden():
    small = 100 * 2**20
    assert "❌ 1080p" in captions.info_caption(INFO, small)
    labels = [b.text for row in formats_kb(INFO, small).inline_keyboard for b in row]
    assert "📹 1080p" not in labels and "📹 720p" in labels


def test_formats_keyboard_layout():
    rows = formats_kb(INFO, MAX).inline_keyboard
    assert all(len(r) <= 3 for r in rows)
    assert rows[-2][0].text == "🔉 MP3"
    assert [b.text for b in rows[-1]] == ["🖼", "📝"]
    for row in rows:
        for b in row:
            assert len(b.callback_data.encode()) <= 64


def test_languages_keyboard():
    rows = languages_kb(INFO, "1080").inline_keyboard
    flat = [b for row in rows for b in row]
    assert flat[0].text == "🎬 Original" and parse_cb(flat[0].callback_data) == (
        "dl",
        "yt",
        INFO.id,
        "1080",
        "",
    )
    de = next(b for b in flat if b.text == "DE-DE")
    assert parse_cb(de.callback_data)[4] == "de-DE"
    assert flat[-1].text == "◀️"
    assert "EN-US" not in [b.text for b in flat]  # original track is the "Original" button


def test_result_kb():
    b = result_kb("yt", INFO.id).inline_keyboard[0][0]
    assert parse_cb(b.callback_data)[:4] == ("music", "yt", INFO.id, "mp3")


def test_progress_text():
    assert "▓░░░░░░░░░ 10%" in captions.progress_text(10, 100)
    assert "100%" in captions.progress_text(150, 100)
    assert "Downloading" in captions.progress_text(5, None)
    assert "(2/4 files)" in captions.progress_text(2, 4, unit="files")


def test_welcome_lists_platforms():
    text = captions.welcome(PLATFORMS, "bot")
    assert all(name in text for name in ("YouTube", "Instagram", "Threads", "SoundCloud", "Spotify"))
    assert "coming soon" not in text
