"""Redis-backed state: media info cache, source urls, Telegram file_id cache, per-user locks."""

from __future__ import annotations

import json
from functools import lru_cache

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import get_settings
from app.models import MediaInfo

SRC_TTL = 30 * 24 * 3600


@lru_cache
def client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
        retry_on_timeout=False,
        retry=Retry(NoBackoff(), 0),
    )


def namespace() -> str:
    return get_settings().bot_token.split(":", 1)[0]


def _fid_key(platform: str, media_id: str, fmt: str, lang: str | None) -> str:
    return f"fid:{namespace()}:{platform}:{media_id}:{fmt}:{lang or 'orig'}"


def save_info(info: MediaInfo) -> None:
    pipe = client().pipeline()
    pipe.set(f"info:{info.platform}:{info.id}", json.dumps(info.to_dict()), ex=get_settings().info_cache_ttl)
    # source url outlives the info cache so old buttons can re-extract
    pipe.set(f"src:{info.platform}:{info.id}", info.url, ex=SRC_TTL)
    pipe.execute()


def load_info(platform: str, media_id: str) -> MediaInfo | None:
    raw = client().get(f"info:{platform}:{media_id}")
    return MediaInfo.from_dict(json.loads(raw)) if raw else None


def load_src(platform: str, media_id: str) -> str | None:
    return client().get(f"src:{platform}:{media_id}")


def save_file_id(platform: str, media_id: str, fmt: str, lang: str | None, file_id: str) -> None:
    """file_id is a Telegram file_id, or a JSON list of [type, file_id] for galleries."""
    client().set(_fid_key(platform, media_id, fmt, lang), file_id)


def get_file_id(platform: str, media_id: str, fmt: str, lang: str | None) -> str | None:
    return client().get(_fid_key(platform, media_id, fmt, lang))


def delete_file_id(platform: str, media_id: str, fmt: str, lang: str | None) -> None:
    client().delete(_fid_key(platform, media_id, fmt, lang))
