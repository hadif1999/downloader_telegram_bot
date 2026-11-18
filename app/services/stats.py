"""Usage statistics and admin state in Redis, for the hidden /admin panel.

Everything here is best effort: recording never raises into a handler or a download, and the
writes are compact, because Redis runs with `appendfsync always` inside a 96 MB container.

Keys (all under ``st:<bot id>:``):
    first          HASH  user -> first-seen timestamp (HLEN = all users ever)
    seen           ZSET  user -> last-seen timestamp (active windows, broadcast recipients)
    prof           HASH  user -> "name\\x1fusername\\x1flanguage code"
    uname          HASH  lowercase username -> user (lookup by @username)
    dau:<day>      HLL   users active that local day (kept DAU_DAYS)
    hau:<hour>     HLL   users active that local hour (kept HAU_HOURS)
    new:<day>      STR   users seen for the first time that day
    blocked        SET   users who blocked the bot (removed when they come back)
    banned         SET   users the admin banned
    chats          ZSET  group/channel chat -> last-seen timestamp; chatname HASH chat -> title
    dl:<day>       HASH  download outcomes that day: <outcome>, p:<platform>:<outcome>, bytes, secs
    dlh:<hour>     HASH  download outcomes that hour
    udl            ZSET  user -> delivered downloads (sent + cached resends)
    errors         LIST  latest failures, JSON, newest first (ERRORS_KEPT)
    maintenance    STR   notice shown to users while maintenance mode is on
    node:<role>:<host>   STR  JSON heartbeat of a bot/worker process (app/services/health.py)
    live:<request>       STR  JSON snapshot of a running download, refreshed by its worker
    nodes / lives        ZSET indexes of the two above by refresh time (no keyspace SCAN)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.services import clock
from app.services.cache import client, namespace

log = logging.getLogger(__name__)

DAU_DAYS = 120
HAU_HOURS = 72
DL_DAYS = 400
DLH_HOURS = 72
ERRORS_KEPT = 50
ACTIVITY_EVERY = 300  # seconds between two activity writes of one user (in-process throttle)
THROTTLE_LIMIT = 50_000
SEP = "\x1f"
OUTCOMES = ("ok", "cached", "fail", "cancel", "uncertain")
WINDOWS = (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400))


def key(name: str) -> str:
    return f"st:{namespace()}:{name}"


# ------------------------------------------------------------------ recording


_recent: dict[int, tuple[float, str, str]] = {}  # user -> (last write, hour bucket, profile)
_recent_lock = threading.Lock()


def _due(user_id: int, now: float, hour: str, profile: str) -> bool:
    with _recent_lock:
        last = _recent.get(user_id)
        if last and now - last[0] < ACTIVITY_EVERY and last[1] == hour and last[2] == profile:
            return False
        if len(_recent) >= THROTTLE_LIMIT:
            _recent.clear()  # crude bound; the next update of each user just writes again
        _recent[user_id] = (now, hour, profile)
        return True


def forget_throttle() -> None:
    with _recent_lock:
        _recent.clear()


def record_activity(
    user_id: int,
    name: str = "",
    username: str | None = None,
    language: str | None = None,
    chat: tuple[int, str, str] | None = None,
    now: float | None = None,
) -> bool:
    """Note that a user interacted (at most once per ACTIVITY_EVERY, or when the hour or their
    name changes). `chat` = (id, type, title) of a group the update came from. Returns whether
    anything was written. Blocking: call in a thread."""
    now = time.time() if now is None else now
    hour, day = clock.hour_key(now), clock.day_key(now)
    profile = SEP.join([(name or "")[:64].replace(SEP, " "), username or "", (language or "")[:8]])
    if not _due(user_id, now, hour, profile):
        return False
    r = client()
    pipe = r.pipeline(transaction=False)
    pipe.hsetnx(key("first"), user_id, int(now))
    pipe.zadd(key("seen"), {user_id: int(now)})
    pipe.hset(key("prof"), user_id, profile)
    if username:
        pipe.hset(key("uname"), username.lower(), user_id)
    pipe.pfadd(key(f"dau:{day}"), user_id)
    pipe.expire(key(f"dau:{day}"), DAU_DAYS * 86400)
    pipe.pfadd(key(f"hau:{hour}"), user_id)
    pipe.expire(key(f"hau:{hour}"), HAU_HOURS * 3600)
    pipe.srem(key("blocked"), user_id)
    if chat is not None:
        chat_id, _kind, title = chat
        pipe.zadd(key("chats"), {chat_id: int(now)})
        pipe.hset(key("chatname"), chat_id, (title or "")[:64])
    first = pipe.execute()[0]
    if first:
        pipe = r.pipeline(transaction=False)
        pipe.incr(key(f"new:{day}"))
        pipe.expire(key(f"new:{day}"), DL_DAYS * 86400)
        pipe.execute()
    return True


def record_chat_status(chat_id: int, title: str, present: bool, now: float | None = None) -> None:
    """The bot was added to (or removed from) a group or channel."""
    if present:
        client().zadd(key("chats"), {chat_id: int(time.time() if now is None else now)})
        client().hset(key("chatname"), chat_id, (title or "")[:64])
    else:
        client().zrem(key("chats"), chat_id)
        client().hdel(key("chatname"), chat_id)


def mark_blocked(user_id: int, blocked: bool = True) -> None:
    """The user blocked (or unblocked) the bot in their private chat."""
    if blocked:
        client().sadd(key("blocked"), user_id)
    else:
        client().srem(key("blocked"), user_id)


def record_download(
    platform: str,
    outcome: str,
    user_id: int | None = None,
    size: int = 0,
    seconds: float = 0.0,
    error: str = "",
    now: float | None = None,
) -> None:
    """One finished request: outcome is ok, cached, fail, cancel or uncertain. Never raises."""
    if outcome not in OUTCOMES:
        return
    try:
        now = time.time() if now is None else now
        day, hour = key(f"dl:{clock.day_key(now)}"), key(f"dlh:{clock.hour_key(now)}")
        pipe = client().pipeline(transaction=False)
        for bucket, ttl in ((day, DL_DAYS * 86400), (hour, DLH_HOURS * 3600)):
            pipe.hincrby(bucket, outcome, 1)
            if bucket == day:
                pipe.hincrby(bucket, f"p:{platform}:{outcome}", 1)
            if size > 0:
                pipe.hincrby(bucket, "bytes", int(size))
            if seconds > 0 and outcome == "ok":
                pipe.hincrbyfloat(bucket, "secs", round(seconds, 2))
            pipe.expire(bucket, ttl)
        if user_id and outcome in {"ok", "cached"}:
            pipe.zincrby(key("udl"), 1, user_id)
        if outcome in {"fail", "uncertain"}:
            entry = {"t": int(now), "p": platform, "o": outcome, "e": (error or "")[:200], "u": user_id}
            pipe.lpush(key("errors"), json.dumps(entry, ensure_ascii=False))
            pipe.ltrim(key("errors"), 0, ERRORS_KEPT - 1)
        pipe.execute()
    except Exception:
        log.warning("couldn't record download statistics", exc_info=True)


# ------------------------------------------------------------------ admin state


def banned_ids() -> set[int]:
    return {int(v) for v in client().smembers(key("banned"))}


def set_banned(user_id: int, banned: bool) -> None:
    if banned:
        client().sadd(key("banned"), user_id)
    else:
        client().srem(key("banned"), user_id)


def maintenance() -> str | None:
    return client().get(key("maintenance"))


def set_maintenance(notice: str | None) -> None:
    if notice:
        client().set(key("maintenance"), notice[:500])
    else:
        client().delete(key("maintenance"))


# ------------------------------------------------------------------ queries


@dataclass
class Profile:
    id: int
    name: str = ""
    username: str = ""
    language: str = ""
    first: int | None = None
    last: int | None = None
    downloads: int = 0
    blocked: bool = False
    banned: bool = False

    @property
    def known(self) -> bool:
        return self.first is not None


def _split_profile(raw: str | None) -> tuple[str, str, str]:
    parts = ((raw or "").split(SEP) + ["", "", ""])[:3]
    return parts[0], parts[1], parts[2]


def profile(user_id: int) -> Profile:
    r = client()
    pipe = r.pipeline(transaction=False)
    pipe.hget(key("first"), user_id)
    pipe.zscore(key("seen"), user_id)
    pipe.hget(key("prof"), user_id)
    pipe.zscore(key("udl"), user_id)
    pipe.sismember(key("blocked"), user_id)
    pipe.sismember(key("banned"), user_id)
    first, last, raw, downloads, blocked, banned = pipe.execute()
    name, username, language = _split_profile(raw)
    return Profile(
        user_id,
        name,
        username,
        language,
        int(first) if first else None,
        int(last) if last else None,
        int(downloads or 0),
        bool(blocked),
        bool(banned),
    )


def find_user(query: str) -> int | None:
    """A numeric ID, @username or t.me/username of a user who has used the bot."""
    text = query.strip().removeprefix("https://").removeprefix("t.me/").lstrip("@").strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    found = client().hget(key("uname"), text.lower())
    if not found:
        return None
    # A changed username leaves its old index entry behind: trust it only while it still matches.
    _, username, _ = _split_profile(client().hget(key("prof"), found))
    return int(found) if username.lower() == text.lower() else None


@dataclass
class Overview:
    users: int = 0
    active: dict[str, int] = field(default_factory=dict)
    new_today: int = 0
    new_yesterday: int = 0
    dau_today: int = 0
    blocked: int = 0
    banned: int = 0
    groups: int = 0
    groups_active: int = 0
    today: dict[str, float] = field(default_factory=dict)
    yesterday: dict[str, float] = field(default_factory=dict)


def _numbers(raw: dict) -> dict[str, float]:
    out = {}
    for name, value in (raw or {}).items():
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def overview(now: float | None = None) -> Overview:
    now = time.time() if now is None else now
    today, yesterday = clock.day_key(now), clock.day_key(now - 86400)
    r = client()
    pipe = r.pipeline(transaction=False)
    pipe.hlen(key("first"))
    for _, seconds in WINDOWS:
        pipe.zcount(key("seen"), now - seconds, "+inf")
    pipe.get(key(f"new:{today}"))
    pipe.get(key(f"new:{yesterday}"))
    pipe.pfcount(key(f"dau:{today}"))
    pipe.scard(key("blocked"))
    pipe.scard(key("banned"))
    pipe.zcard(key("chats"))
    pipe.zcount(key("chats"), now - 7 * 86400, "+inf")
    pipe.hgetall(key(f"dl:{today}"))
    pipe.hgetall(key(f"dl:{yesterday}"))
    values = pipe.execute()
    users, rest = values[0], values[1:]
    active = {label: int(count) for (label, _), count in zip(WINDOWS, rest[: len(WINDOWS)], strict=True)}
    (new_today, new_yesterday, dau, blocked, banned, groups, groups_active, dl_today, dl_yesterday) = rest[
        len(WINDOWS) :
    ]
    return Overview(
        int(users),
        active,
        int(new_today or 0),
        int(new_yesterday or 0),
        int(dau),
        int(blocked),
        int(banned),
        int(groups),
        int(groups_active),
        _numbers(dl_today),
        _numbers(dl_yesterday),
    )


@dataclass
class Day:
    label: str  # e.g. "11-18"
    active: int
    new: int
    downloads: dict[str, float]


def daily(days: int = 14, now: float | None = None) -> list[Day]:
    moments = clock.days_back(days, now)
    pipe = client().pipeline(transaction=False)
    for moment in moments:
        day = moment.strftime("%Y%m%d")
        pipe.pfcount(key(f"dau:{day}"))
        pipe.get(key(f"new:{day}"))
        pipe.hgetall(key(f"dl:{day}"))
    values = pipe.execute()
    return [
        Day(
            moment.strftime("%m-%d"),
            int(values[i * 3]),
            int(values[i * 3 + 1] or 0),
            _numbers(values[i * 3 + 2]),
        )
        for i, moment in enumerate(moments)
    ]


@dataclass
class Hour:
    label: str  # local hour, e.g. "14"
    active: int
    downloads: dict[str, float]


def hourly(hours: int = 24, now: float | None = None) -> list[Hour]:
    moments = clock.hours_back(hours, now)
    pipe = client().pipeline(transaction=False)
    for moment in moments:
        bucket = moment.strftime("%Y%m%d%H")
        pipe.pfcount(key(f"hau:{bucket}"))
        pipe.hgetall(key(f"dlh:{bucket}"))
    values = pipe.execute()
    return [
        Hour(moment.strftime("%H"), int(values[i * 2]), _numbers(values[i * 2 + 1]))
        for i, moment in enumerate(moments)
    ]


def unique_active(days: int, now: float | None = None) -> int:
    """Distinct users active over the last `days` local days (union of the daily HLLs)."""
    keys = [key(f"dau:{m.strftime('%Y%m%d')}") for m in clock.days_back(days, now)]
    return int(client().pfcount(*keys))


def platforms(days: int = 7, now: float | None = None) -> dict[str, dict[str, int]]:
    """platform -> outcome -> count over the last `days` local days."""
    pipe = client().pipeline(transaction=False)
    for moment in clock.days_back(days, now):
        pipe.hgetall(key(f"dl:{moment.strftime('%Y%m%d')}"))
    totals: dict[str, dict[str, int]] = {}
    for raw in pipe.execute():
        for name, value in (raw or {}).items():
            if not name.startswith("p:"):
                continue
            _, platform, outcome = name.split(":", 2)
            bucket = totals.setdefault(platform, {})
            bucket[outcome] = bucket.get(outcome, 0) + int(value)
    return totals


def top_users(limit: int = 10) -> list[tuple[Profile, int]]:
    rows = client().zrevrange(key("udl"), 0, limit - 1, withscores=True)
    if not rows:
        return []
    names = client().hmget(key("prof"), [user for user, _ in rows])
    result = []
    for (user, score), raw in zip(rows, names, strict=True):
        name, username, language = _split_profile(raw)
        result.append((Profile(int(user), name, username, language), int(score)))
    return result


def recent_errors(limit: int = 10) -> list[dict]:
    out = []
    for raw in client().lrange(key("errors"), 0, limit - 1):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def recipients(since: float | None = None, batch: int = 500) -> Iterator[int]:
    """User IDs to broadcast to (active since `since`, or everyone), minus blocked and banned."""
    r = client()
    skip = {int(v) for v in r.smembers(key("blocked"))} | banned_ids()
    low = "-inf" if since is None else since
    offset = 0
    while True:
        chunk = r.zrangebyscore(key("seen"), low, "+inf", start=offset, num=batch)
        if not chunk:
            return
        offset += len(chunk)
        for user in chunk:
            if int(user) not in skip:
                yield int(user)


def count_recipients(since: float | None = None) -> int:
    return sum(1 for _ in recipients(since))


def export_users() -> Iterator[list]:
    """CSV rows of every known user: id, username, name, language, first/last seen, downloads,
    blocked, banned."""
    r = client()
    blocked = {int(v) for v in r.smembers(key("blocked"))}
    banned = banned_ids()
    yield ["id", "username", "name", "language", "first_seen", "last_seen", "downloads", "blocked", "banned"]
    cursor = 0
    while True:
        cursor, chunk = r.hscan(key("first"), cursor, count=500)
        if chunk:
            users = list(chunk)
            pipe = r.pipeline(transaction=False)
            pipe.hmget(key("prof"), users)
            for user in users:
                pipe.zscore(key("seen"), user)
            for user in users:
                pipe.zscore(key("udl"), user)
            values = pipe.execute()
            profiles, seen, downloads = values[0], values[1 : 1 + len(users)], values[1 + len(users) :]
            for i, user in enumerate(users):
                name, username, language = _split_profile(profiles[i])
                uid = int(user)
                yield [
                    uid,
                    username,
                    name,
                    language,
                    _iso(chunk[user]),
                    _iso(seen[i]),
                    int(downloads[i] or 0),
                    int(uid in blocked),
                    int(uid in banned),
                ]
        if cursor == 0:
            return


def _iso(ts) -> str:
    if not ts:
        return ""
    return clock.local(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ live downloads and nodes


def _publish(index: str, member: str, payload: dict, ttl: int) -> None:
    """A JSON entry with a TTL, listed in a sorted-set index by its refresh time (no SCAN)."""
    now = time.time()
    pipe = client().pipeline(transaction=False)
    pipe.set(key(f"{index}:{member}"), json.dumps(payload, ensure_ascii=False), ex=ttl)
    pipe.zadd(key(f"{index}s"), {member: now})
    pipe.zremrangebyscore(key(f"{index}s"), "-inf", now - 6 * ttl)
    pipe.expire(key(f"{index}s"), 6 * ttl)
    pipe.execute()


def _listed(index: str, ttl: int, limit: int) -> list[dict]:
    r = client()
    members = r.zrangebyscore(key(f"{index}s"), time.time() - ttl, "+inf", start=0, num=limit)
    if not members:
        return []
    out = []
    for member, raw in zip(members, r.mget([key(f"{index}:{m}") for m in members]), strict=True):
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        item.setdefault("id", member)
        out.append(item)
    return out


LIVE_TTL = 30
NODE_TTL = 90


def publish_live(request_id: str, snapshot: dict) -> None:
    try:
        _publish("live", request_id, snapshot, LIVE_TTL)
    except Exception:
        log.debug("couldn't publish live download state", exc_info=True)


def clear_live(request_id: str) -> None:
    try:
        pipe = client().pipeline(transaction=False)
        pipe.delete(key(f"live:{request_id}"))
        pipe.zrem(key("lives"), request_id)
        pipe.execute()
    except Exception:
        log.debug("couldn't clear live download state", exc_info=True)


def live_downloads(limit: int = 50) -> list[dict]:
    return sorted(_listed("live", LIVE_TTL, limit), key=lambda item: item.get("started", 0))


def publish_node(name: str, snapshot: dict) -> None:
    _publish("node", name, snapshot, NODE_TTL)


def nodes(limit: int = 20) -> list[dict]:
    return sorted(
        _listed("node", NODE_TTL, limit), key=lambda item: (item.get("role", ""), item.get("host", ""))
    )
