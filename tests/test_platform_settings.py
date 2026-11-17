import runpy
from pathlib import Path

import pytest

from app import config
from app.platforms import registry
from app.services import captions, media

PLATFORMS = [
    ("YouTube", "yt", "https://youtu.be/0MNVhXEX9to"),
    ("Instagram", "ig", "https://instagram.com/p/abc/"),
    ("TikTok", "tt", "https://vm.tiktok.com/abc/"),
    ("Pinterest", "pin", "https://pin.it/abc"),
    ("Snapchat", "sc", "https://snapchat.com/spotlight/abc"),
    ("Likee", "lk", "https://likee.video/@user/video/123"),
    ("VK", "vk", "https://vk.com/video-1_2"),
    ("Facebook", "fb", "https://fb.watch/abc/"),
    ("Threads", "th", "https://threads.net/@user/post/abc"),
    ("SoundCloud", "scl", "https://soundcloud.com/user/track"),
    ("Spotify", "sp", "https://open.spotify.com/track/abc"),
]


@pytest.fixture(autouse=True)
def clear_platform_env(monkeypatch):
    for title, _, _ in PLATFORMS:
        monkeypatch.delenv(f"{title.upper()}_ENABLED", raising=False)


@pytest.mark.parametrize("title,key,url", PLATFORMS)
def test_platform_env_switch(monkeypatch, title, key, url):
    for platform_title, _, _ in PLATFORMS:
        monkeypatch.setenv(f"{platform_title.upper()}_ENABLED", "true")
    monkeypatch.setenv(f"{title.upper()}_ENABLED", "false")
    settings = config.Settings(_env_file=None)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    disabled = runpy.run_path(str(Path(registry.__file__)))

    assert disabled["detect"](url) is None
    assert title not in captions.welcome(disabled["PLATFORMS"], "bot")
    assert len(disabled["PLATFORMS"]) == len(PLATFORMS) - 1
    with pytest.raises(KeyError):
        disabled["get"](key)

    monkeypatch.setenv(f"{title.upper()}_ENABLED", "true")
    settings = config.Settings(_env_file=None)
    enabled = runpy.run_path(str(Path(registry.__file__)))
    assert enabled["detect"](url)[0].name == key
    assert title in captions.welcome(enabled["PLATFORMS"], "bot")


def test_all_platforms_can_be_disabled(monkeypatch):
    for title, _, _ in PLATFORMS:
        monkeypatch.setenv(f"{title.upper()}_ENABLED", "false")
    monkeypatch.setattr(config, "get_settings", lambda: config.Settings(_env_file=None))
    disabled = runpy.run_path(str(Path(registry.__file__)))
    assert disabled["PLATFORMS"] == []
    assert disabled["detect"]("https://youtu.be/0MNVhXEX9to") is None


def test_unspecified_platforms_are_disabled(monkeypatch):
    settings = config.Settings(_env_file=None)
    assert all(getattr(settings, f"{title.lower()}_enabled") is False for title, _, _ in PLATFORMS)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert runpy.run_path(str(Path(registry.__file__)))["PLATFORMS"] == []


def test_dotenv_platform_flags(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("YOUTUBE_ENABLED=false\nINSTAGRAM_ENABLED=true\n")
    settings = config.Settings(_env_file=env_file)
    assert settings.youtube_enabled is False
    assert settings.instagram_enabled is True
    assert settings.spotify_enabled is False


def test_disabled_platform_cannot_use_cached_metadata(monkeypatch):
    monkeypatch.setattr(registry, "_BY_NAME", {})

    def unexpected_cache_read(*args):
        pytest.fail("Disabled platforms must be rejected before reading cached metadata")

    monkeypatch.setattr(media.cache, "load_info", unexpected_cache_read)
    with pytest.raises(KeyError):
        media.resolve("yt", "0MNVhXEX9to")


def test_youtube_js_runtimes_setting(monkeypatch):
    from app.config import Settings

    assert Settings(bot_token="1:x").youtube_js_runtimes == []
    assert Settings(bot_token="1:x", youtube_js_runtimes="deno, node").youtube_js_runtimes == ["deno", "node"]
    import pytest

    with pytest.raises(ValueError, match="unknown runtime"):
        Settings(bot_token="1:x", youtube_js_runtimes="python")


def test_js_runtimes_reach_ytdlp(monkeypatch):
    from types import SimpleNamespace as NS

    from app.platforms import ytdlp

    real = ytdlp.get_settings()
    monkeypatch.setattr(
        ytdlp, "get_settings", lambda: NS(**{**real.__dict__, "youtube_js_runtimes": ["deno"]})
    )
    assert ytdlp.YtDlpPlatform().ydl_opts()["js_runtimes"] == {"deno": {}}
