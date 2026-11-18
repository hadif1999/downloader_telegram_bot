"""Operational views for /admin: the Celery queue in Redis, Redis itself, admin cancellation."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from app.services import requests
from app.services.cache import client

log = logging.getLogger(__name__)
QUEUE = "celery"  # kombu's default queue list (the broker is the same Redis)
UNACKED = "unacked"  # delivered, not yet acknowledged (task_acks_late: running or reserved)


@dataclass
class QueuedTask:
    id: str
    name: str
    platform: str = ""
    fmt: str = ""
    user: int | None = None
    chat: int | None = None
    message: int | None = None
    is_caption: bool = False
    batch: bool = False


def _decode(raw: str) -> QueuedTask | None:
    try:
        message = json.loads(raw)
        headers = message.get("headers") or {}
        name, task_id = headers.get("task", "?"), headers.get("id", "")
        body = message.get("body", "")
        if (message.get("properties") or {}).get("body_encoding") == "base64":
            body = base64.b64decode(body).decode()
        args, kwargs = (json.loads(body) + [[], {}])[:2] if body else ([], {})
    except (ValueError, TypeError, AttributeError):
        return None
    task = QueuedTask(task_id, name)
    if name == "download_media" and len(args) >= 8:
        task.chat, task.message, task.is_caption = args[0], args[1], bool(args[2])
        task.platform, task.fmt, task.user = str(args[3]), str(args[5]), args[7]
        task.batch = bool((kwargs or {}).get("batch"))
    return task


def queue_length() -> int:
    return int(client().llen(QUEUE))


def in_flight() -> int:
    return int(client().hlen(UNACKED))


def queued(limit: int = 20) -> list[QueuedTask]:
    """The next tasks to run, in order (workers pop from the right end of the list)."""
    raw = client().lrange(QUEUE, -limit, -1)
    return [task for task in (_decode(item) for item in reversed(raw)) if task is not None]


def queue_summary() -> dict[str, int]:
    """Pending tasks by name (whole queue, decoded in pages)."""
    r = client()
    counts: dict[str, int] = {}
    total = r.llen(QUEUE)
    for start in range(0, min(total, 5000), 500):
        for raw in r.lrange(QUEUE, start, start + 499):
            task = _decode(raw)
            name = task.name if task else "?"
            counts[name] = counts.get(name, 0) + 1
    return counts


def redis_info() -> dict:
    info = client().info()
    keys = client().dbsize()
    return {
        "version": info.get("redis_version"),
        "uptime": info.get("uptime_in_seconds"),
        "memory": info.get("used_memory"),
        "peak": info.get("used_memory_peak"),
        "maxmemory": info.get("maxmemory") or None,
        "clients": info.get("connected_clients"),
        "ops": info.get("instantaneous_ops_per_sec"),
        "keys": keys,
        "aof": bool(info.get("aof_enabled")),
        "hits": info.get("keyspace_hits"),
        "misses": info.get("keyspace_misses"),
    }


def cancel(request_id: str, user_id: int) -> int:
    """Cancel a user's request on the admin's behalf (same outcomes as the user's Cancel button:
    the request's journal still decides whether it is too late)."""
    return requests.request_cancel(request_id, user_id)
