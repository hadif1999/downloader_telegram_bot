"""Redis-backed state: media info cache, Telegram file_id cache, per-user locks."""

from __future__ import annotations

import json
from functools import lru_cache

import redis

from app.config import get_settings
from app.models import MediaInfo


@lru_cache
def client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def _info_key(platform: str, media_id: str) -> str:
    return f"info:{platform}:{media_id}"


def _fid_key(platform: str, media_id: str, fmt: str, lang: str | None) -> str:
    return f"fid:{platform}:{media_id}:{fmt}:{lang or 'orig'}"


def save_info(info: MediaInfo) -> None:
    client().set(
        _info_key(info.platform, info.id),
        json.dumps(info.to_dict()),
        ex=get_settings().info_cache_ttl,
    )


def load_info(platform: str, media_id: str) -> MediaInfo | None:
    raw = client().get(_info_key(platform, media_id))
    return MediaInfo.from_dict(json.loads(raw)) if raw else None


def save_file_id(platform: str, media_id: str, fmt: str, lang: str | None, file_id: str) -> None:
    client().set(_fid_key(platform, media_id, fmt, lang), file_id)


def get_file_id(platform: str, media_id: str, fmt: str, lang: str | None) -> str | None:
    return client().get(_fid_key(platform, media_id, fmt, lang))


def acquire_user_lock(user_id: int) -> bool:
    return bool(client().set(f"lock:user:{user_id}", "1", nx=True, ex=get_settings().task_time_limit))


def release_user_lock(user_id: int) -> None:
    client().delete(f"lock:user:{user_id}")
