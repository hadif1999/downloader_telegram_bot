from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    # Local Telegram Bot API server (allows uploads up to 2 GB). Empty = official cloud API.
    bot_api_url: str = "http://telegram-bot-api:8081"
    bot_api_local: bool = True

    redis_url: str = "redis://redis:6379/0"
    max_parallel_downloads: int = 2
    max_file_mb: int = 2000
    task_time_limit: int = 60 * 60
    download_dir: Path = Path("/downloads")

    # Netscape cookie files named <platform>.txt (youtube.txt, instagram.txt, facebook.txt, ...)
    cookies_dir: Path = Path("/cookies")
    # bgutil PO-token provider sidecar (empty disables)
    pot_provider_url: str = "http://bgutil-provider:4416"

    info_cache_ttl: int = 60 * 60

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
