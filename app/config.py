import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MAX_FORCE_JOIN_CHANNELS = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    # Comma-separated channels a user must join before using the bot (empty disables the gate).
    force_join_channel_id: Annotated[list[int | str], NoDecode] = []
    # Optional comma-separated join links, matched to the channel IDs by position; leave a
    # position empty for a public channel (its t.me/username link is derived).
    force_join_channel_url: Annotated[list[str], NoDecode] = []

    @field_validator("force_join_channel_id", mode="before")
    @classmethod
    def channel_ids(cls, value):
        channels = []
        for item in _items(value):
            if str(item).strip() == "":
                # URLs are matched by position, so a gap would silently shift them.
                raise ValueError("FORCE_JOIN_CHANNEL_ID contains an empty entry between commas")
            if isinstance(item, bool):
                raise ValueError("FORCE_JOIN_CHANNEL_ID entries must be channel IDs or usernames")
            text = str(item).strip()
            if re.fullmatch(r"-[1-9]\d*", text):
                channel = int(text)
            elif re.fullmatch(r"@[A-Za-z0-9_]{1,32}", text):
                channel = text
            else:
                raise ValueError(
                    "FORCE_JOIN_CHANNEL_ID entries must be negative channel IDs or @channel_usernames, "
                    "separated by commas"
                )
            if channel in channels:
                raise ValueError(f"FORCE_JOIN_CHANNEL_ID lists {channel} more than once")
            channels.append(channel)
        if len(channels) > MAX_FORCE_JOIN_CHANNELS:
            raise ValueError(f"FORCE_JOIN_CHANNEL_ID accepts at most {MAX_FORCE_JOIN_CHANNELS} channels")
        return channels

    @field_validator("force_join_channel_url", mode="before")
    @classmethod
    def channel_urls(cls, value):
        urls = [str(item).strip() for item in _items(value)]
        for text in urls:
            if not text:
                continue
            url = urlsplit(text)
            if (
                url.scheme != "https"
                or url.netloc.lower() not in {"t.me", "telegram.me"}
                or url.query
                or url.fragment
                or not re.fullmatch(
                    r"/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z0-9_]+)/?", url.path
                )
            ):
                raise ValueError("FORCE_JOIN_CHANNEL_URL entries must be HTTPS Telegram channel/invite links")
        return urls

    @model_validator(mode="after")
    def channel_urls_match_ids(self):
        if len(self.force_join_channel_url) > len(self.force_join_channel_id):
            raise ValueError(
                "FORCE_JOIN_CHANNEL_URL has more entries than FORCE_JOIN_CHANNEL_ID; "
                "links are matched to channels by position"
            )
        return self

    def force_join_url(self, index: int) -> str:
        urls = self.force_join_channel_url
        return urls[index] if index < len(urls) else ""

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
    # Downloads are fetched in byte-range parts of this size, flushed to disk and dropped from the
    # page cache after each part, so memory use stays flat even for 2 GB files.
    download_part_mb: int = Field(default=200, ge=1, le=2000)
    # Recently downloaded files kept on disk for retries (LRU); 0 disables retention.
    media_cache_items: int = Field(default=3, ge=0, le=100)
    media_cache_max_mb: int = Field(default=4096, ge=0)
    media_cache_ttl: int = Field(default=6 * 3600, ge=60, le=7 * 24 * 3600)
    # Free space kept on the download volume; new downloads are refused below it.
    min_free_disk_mb: int = Field(default=1024, ge=0)

    # Netscape cookie files named <platform>.txt (youtube.txt, instagram.txt, facebook.txt, ...)
    cookies_dir: Path = Path("/cookies")
    # bgutil PO-token provider sidecar (empty disables)
    pot_provider_url: str = "http://bgutil-provider:4416"

    info_cache_ttl: int = 60 * 60

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def download_part_bytes(self) -> int:
        return self.download_part_mb * 1024 * 1024

    @property
    def max_source_bytes(self) -> int:
        return self.max_source_mb * 1024 * 1024


def _items(value) -> list:
    """Comma-separated string, single value or list -> entries, without trailing blank entries."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = ["" if item is None else item for item in value]
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = [value]
    while items and str(items[-1]).strip() == "":
        items.pop()
    return items


@lru_cache
def get_settings() -> Settings:
    return Settings()
