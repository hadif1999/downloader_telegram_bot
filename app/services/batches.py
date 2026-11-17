"""Batches: a playlist / album / set, or several links from one message, sent one item at a time.

A batch is a Redis hash: static `meta` and `items` (JSON) written once, plus small fields that Lua
scripts change atomically:

    phase    draft -> running -> (stopping ->) done | stopped;  draft -> cancelled
    sel      one character per item, "1" = selected (toggled while in draft)
    quality  preset for video items (media.PRESETS)
    cursor   index of the item most recently started (-1 before the first)
    r:<i>    outcome of item i: "<code>|<reason>"

Items run strictly one after another under the user's normal download lock: `begin(after)` hands
out the next selected item only while `cursor == after`, so a duplicate "advance" (Celery
redelivery, a worker finishing twice) is a no-op instead of a second parallel download. Each item
is an ordinary request with its own journal, so uncertain deliveries are never resent.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.services import cache

# Quality presets for the video items of a batch: a height cap, or MP3 for everything.
PRESETS = ("1080", "720", "480", "mp3")
DEFAULT_PRESET = "720"
PICKER_PAGE = 8  # items per page of the "Choose items" checklist

SENT, CACHED, FAILED, SKIPPED, UNCERTAIN = "sent", "cached", "failed", "skipped", "uncertain"

# begin() results that are not an item index
FINISHED, DUPLICATE, GONE = -1, -2, -3


class Busy(Exception):
    """The user has another download running; the batch waits and tries again."""


@dataclass
class Item:
    platform: str
    media_id: str
    url: str
    title: str
    duration: int | None = None
    kind: str | None = None  # "video" | "audio" | "gallery" when already looked up
    uploader: str | None = None

    def to_json(self) -> list:
        return [self.platform, self.media_id, self.url, self.title, self.duration, self.kind, self.uploader]

    @classmethod
    def from_json(cls, data: list) -> Item:
        return cls(*data)


@dataclass
class Batch:
    id: str
    user: int
    chat: int
    card: int  # message id of the summary / progress card (a text message)
    reply_to: int | None
    lang: str
    items: list[Item]
    meta: dict
    selected: str
    quality: str
    phase: str
    cursor: int
    results: dict[int, tuple[str, str]] = field(default_factory=dict)

    @property
    def chosen(self) -> list[int]:
        return [i for i, c in enumerate(self.selected) if c == "1"]

    def outcome(self, index: int) -> str | None:
        result = self.results.get(index)
        return result[0] if result else None

    def count(self, *codes: str) -> int:
        return sum(1 for code, _ in self.results.values() if code in codes)


def key(batch_id: str) -> str:
    return f"batch:{cache.namespace()}:{batch_id}"


def create(
    *,
    user: int,
    chat: int,
    card: int,
    reply_to: int | None,
    lang: str,
    items: list[Item],
    meta: dict,
    quality: str,
) -> str:
    batch_id = uuid.uuid4().hex[:12]
    pipe = cache.client().pipeline()
    pipe.hset(
        key(batch_id),
        mapping={
            "user": user,
            "chat": chat,
            "card": card,
            "reply": reply_to or "",
            "lang": lang,
            "items": json.dumps([i.to_json() for i in items]),
            "meta": json.dumps(meta),
            "sel": "1" * len(items),
            "quality": quality,
            "phase": "draft",
            "cursor": -1,
        },
    )
    pipe.expire(key(batch_id), get_settings().info_cache_ttl)
    pipe.execute()
    return batch_id


def load(batch_id: str) -> Batch | None:
    raw = cache.client().hgetall(key(batch_id))
    if not raw or "items" not in raw:
        return None
    results = {}
    for name, value in raw.items():
        if name.startswith("r:"):
            code, _, reason = value.partition("|")
            results[int(name[2:])] = (code, reason)
    return Batch(
        id=batch_id,
        user=int(raw["user"]),
        chat=int(raw["chat"]),
        card=int(raw["card"]),
        reply_to=int(raw["reply"]) if raw.get("reply") else None,
        lang=raw.get("lang") or "",
        items=[Item.from_json(i) for i in json.loads(raw["items"])],
        meta=json.loads(raw.get("meta") or "{}"),
        selected=raw.get("sel", ""),
        quality=raw.get("quality", "720"),
        phase=raw.get("phase", ""),
        cursor=int(raw.get("cursor", -1)),
        results=results,
    )


# Return codes shared by the draft operations: 1 ok, 0 expired, -1 not the owner, -2 wrong phase.
OK, EXPIRED, NOT_OWNER, WRONG_PHASE, EMPTY = 1, 0, -1, -2, -3

_EDIT = """
local phase = redis.call('HGET', KEYS[1], 'phase')
if not phase then return 0 end
if redis.call('HGET', KEYS[1], 'user') ~= ARGV[1] then return -1 end
if phase ~= 'draft' then return -2 end
local sel = redis.call('HGET', KEYS[1], 'sel')
if ARGV[2] == 'q' then
    redis.call('HSET', KEYS[1], 'quality', ARGV[3])
    return 1
elseif ARGV[2] == 't' then
    local i = tonumber(ARGV[3])
    if not i or i < 0 or i >= #sel then return -3 end
    local c = (string.sub(sel, i + 1, i + 1) == '1') and '0' or '1'
    sel = string.sub(sel, 1, i) .. c .. string.sub(sel, i + 2)
elseif ARGV[2] == 'all' then
    sel = string.rep('1', #sel)
elseif ARGV[2] == 'none' then
    sel = string.rep('0', #sel)
end
redis.call('HSET', KEYS[1], 'sel', sel)
return 1
"""


def edit(batch_id: str, user: int, op: str, arg: str = "") -> int:
    """Draft changes by the owner: op "t" toggles item `arg`, "all"/"none", "q" sets the quality."""
    return int(cache.client().eval(_EDIT, 1, key(batch_id), user, op, arg))


_START = """
local phase = redis.call('HGET', KEYS[1], 'phase')
if not phase then return 0 end
if redis.call('HGET', KEYS[1], 'user') ~= ARGV[1] then return -1 end
if phase ~= 'draft' then return -2 end
if not string.find(redis.call('HGET', KEYS[1], 'sel'), '1', 1, true) then return -3 end
redis.call('HSET', KEYS[1], 'phase', 'running', 'cursor', -1)
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


def _running_ttl() -> int:
    return get_settings().queue_ttl + 3600


def start(batch_id: str, user: int) -> int:
    return int(cache.client().eval(_START, 1, key(batch_id), user, _running_ttl()))


_BEGIN = """
local phase = redis.call('HGET', KEYS[1], 'phase')
if phase ~= 'running' and phase ~= 'stopping' then return -3 end
if tonumber(redis.call('HGET', KEYS[1], 'cursor')) ~= tonumber(ARGV[1]) then return -2 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
if phase == 'stopping' then
    redis.call('HSET', KEYS[1], 'phase', 'stopped')
    return -1
end
local pos = string.find(redis.call('HGET', KEYS[1], 'sel'), '1', tonumber(ARGV[1]) + 2, true)
if not pos then
    redis.call('HSET', KEYS[1], 'phase', 'done')
    return -1
end
redis.call('HSET', KEYS[1], 'cursor', pos - 1)
return pos - 1
"""


def begin(batch_id: str, after: int) -> int:
    """Claim the next selected item after `after`. Returns its index, FINISHED (the batch just
    ended: draw the final summary), DUPLICATE (someone else already advanced) or GONE."""
    return int(cache.client().eval(_BEGIN, 1, key(batch_id), after, _running_ttl()))


_RELEASE = """
if tonumber(redis.call('HGET', KEYS[1], 'cursor')) ~= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 'cursor', ARGV[2])
return 1
"""


def release(batch_id: str, index: int, after: int) -> bool:
    """Give back a claimed item that couldn't start yet (the user is busy), to claim it later."""
    return bool(cache.client().eval(_RELEASE, 1, key(batch_id), index, after))


def record(batch_id: str, index: int, code: str, reason: str = "") -> None:
    cache.client().hset(key(batch_id), f"r:{index}", f"{code}|{reason}")


_STOP = """
local phase = redis.call('HGET', KEYS[1], 'phase')
if not phase then return 0 end
if redis.call('HGET', KEYS[1], 'user') ~= ARGV[1] then return -1 end
if phase == 'draft' then
    redis.call('HSET', KEYS[1], 'phase', 'cancelled')
    return 2
end
if phase == 'running' then
    redis.call('HSET', KEYS[1], 'phase', 'stopping')
    return 1
end
return -2
"""


def stop(batch_id: str, user: int) -> int:
    """Draft: cancelled (2). Running: no further items start (1); the current one finishes."""
    return int(cache.client().eval(_STOP, 1, key(batch_id), user))
