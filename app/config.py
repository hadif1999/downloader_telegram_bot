import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MAX_FORCE_JOIN_CHANNELS = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    # Interface language for users who haven't picked one with /language.
    default_language: Literal["fa", "en"] = "fa"
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

    @field_validator("youtube_js_runtimes", mode="before")
    @classmethod
    def js_runtimes(cls, value):
        names = [str(item).strip().lower() for item in _items(value) if str(item).strip()]
        unknown = set(names) - {"deno", "node", "bun", "quickjs"}
        if unknown:
            raise ValueError(f"YOUTUBE_JS_RUNTIMES: unknown runtime(s) {sorted(unknown)}")
        return names

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
    # Playlists, albums and sets: at most this many items are offered (the rest are listed as cut).
    collection_max_items: int = Field(default=50, ge=1, le=200)
    # Links read from one message; more are ignored with a note on the summary card.
    message_max_links: int = Field(default=10, ge=1, le=20)
    download_dir: Path = Path("/downloads")
    # Parallel connections per file (IDM/aria2-style segmented downloads). Connections are added one
    # at a time while they still raise the speed; 1 downloads over a single connection.
    download_connections: int = Field(default=4, ge=1, le=8)
    # A range is only split between connections while both halves stay at least this large, so
    # small files use one connection.
    download_min_split_mb: int = Field(default=8, ge=1, le=1024)
    # Upper bound of the adaptive request size (each connection starts at 4 MB and doubles while
    # the speed holds). YouTube is always capped at 10 MB: its large ranges stall.
    download_chunk_max_mb: int = Field(default=64, ge=1, le=1024)
    # Obsolete (fixed 200 MB parts); ignored, with a warning, by the adaptive downloader.
    download_part_mb: int | None = None

    @field_validator("download_part_mb")
    @classmethod
    def obsolete_part_size(cls, value):
        if value is not None:
            logging.getLogger(__name__).warning(
                "DOWNLOAD_PART_MB is obsolete and ignored: part sizes now adapt to each download "
                "(see DOWNLOAD_CONNECTIONS, DOWNLOAD_MIN_SPLIT_MB, DOWNLOAD_CHUNK_MAX_MB)"
            )
        return value

    # JavaScript runtimes yt-dlp may use for YouTube's "n"/signature challenges (comma list, e.g.
    # "deno"); empty disables them. Off by default: solving needs ~320 MB per run, which the 1.5 GB
    # budget can't afford, while downloads stay fast without it (all formats listed, 21 MB/s), and
    # every lookup is ~4.5 s faster. Enable on a larger host if YouTube starts hiding formats.
    youtube_js_runtimes: Annotated[list[str], NoDecode] = []
    # Metadata lookups (link -> format menu) running at once in the bot; the rest wait their turn.
    extract_slots: int = Field(default=4, ge=1, le=16)
    # Retries per part (and per whole download); each retry resumes from the bytes already on disk.
    download_retries: int = Field(default=3, ge=0, le=10)
    # Stall timeout in seconds: an attempt fails when no data arrives for this long.
    download_timeout: float = Field(default=90, ge=5, le=900)
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

    # How long a looked-up link is remembered: the format card (repeat links answer instantly) and
    # its stream URLs (the worker skips a second lookup; capped at 4 h, YouTube URLs expire at ~6 h).
    info_cache_ttl: int = Field(default=2 * 3600, ge=60, le=7 * 24 * 3600)

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def download_min_split_bytes(self) -> int:
        return self.download_min_split_mb * 1024 * 1024

    @property
    def download_chunk_max_bytes(self) -> int:
        return self.download_chunk_max_mb * 1024 * 1024

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
