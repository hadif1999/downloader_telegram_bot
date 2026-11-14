from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""

    youtube_enabled: bool = False
    instagram_enabled: bool = False
    tiktok_enabled: bool = False
    pinterest_enabled: bool = False
    snapchat_enabled: bool = False
    likee_enabled: bool = False
    vk_enabled: bool = False
    facebook_enabled: bool = False
    threads_enabled: bool = False
    soundcloud_enabled: bool = False
    spotify_enabled: bool = False
    # Local Telegram Bot API server (allows uploads up to 2 GB). Empty = official cloud API.
    bot_api_url: str = "http://telegram-bot-api:8081"
    bot_api_local: bool = True

    redis_url: str = "redis://redis:6379/0"
    max_parallel_downloads: int = Field(default=2, ge=1)
    max_file_mb: int = Field(default=2000, ge=1, le=2000)
    max_source_mb: int = Field(default=4096, ge=1)
    task_time_limit: int = Field(default=60 * 60, ge=120, le=24 * 3600)
    queue_ttl: int = Field(default=24 * 3600, ge=60, le=7 * 24 * 3600)
    download_dir: Path = Path("/downloads")

    # Netscape cookie files named <platform>.txt (youtube.txt, instagram.txt, facebook.txt, ...)
    cookies_dir: Path = Path("/cookies")
    # bgutil PO-token provider sidecar (empty disables)
    pot_provider_url: str = "http://bgutil-provider:4416"

    info_cache_ttl: int = 60 * 60

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def max_source_bytes(self) -> int:
        return self.max_source_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
