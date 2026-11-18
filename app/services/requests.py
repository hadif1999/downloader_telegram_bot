"""Redis request journal and owned leases shared by cached sends and Celery workers.

A persisted `sending` batch without a live lease is deliberately not resent: Telegram
may have accepted it before the process died. This closes the replay duplication window
without pretending a transaction across Redis and Telegram is possible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

from app.config import get_settings
from app.services.cache import SRC_TTL, client, namespace

log = logging.getLogger(__name__)
LEASE_SECONDS = 90


class LeaseBusy(Exception):
    pass


class LeaseLost(Exception):
    pass


class DeliveryUncertain(Exception):
    pass


def keys(request_id: str, user_id: int) -> tuple[str, str, str]:
    base = f"request:{namespace()}:{request_id}"
    return base, base + ":lease", f"lock:{namespace()}:user:{user_id}"


def create(request_id: str, user_id: int, media: str = "") -> bool:
    """`media` ("platform|id|fmt|lang") lets the bot redraw the card when a queued job is cancelled."""
    job, _, user = keys(request_id, user_id)
    return bool(
        client().eval(
            """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        if not redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2]) then return 0 end
        redis.call('HSET', KEYS[1], 'phase', 'queued', 'offset', 0, 'sent', '[]', 'media', ARGV[4], 'user', ARGV[5])
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        return 1
    """,
            2,
            job,
            user,
            request_id,
            get_settings().queue_ttl + 60,
            SRC_TTL,
            media,
            user_id,
        )
    )


def load(request_id: str, user_id: int) -> dict:
    return client().hgetall(keys(request_id, user_id)[0])


def prepare_queue(request_id: str, user_id: int) -> bool:
    job, _, user = keys(request_id, user_id)
    return bool(
        client().eval(
            """
        if redis.call('HGET', KEYS[1], 'phase') ~= 'queued' or redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
        redis.call('EXPIRE', KEYS[2], ARGV[2])
        return 1
    """,
            2,
            job,
            user,
            request_id,
            get_settings().queue_ttl + 60,
        )
    )


def claim(request_id: str, user_id: int, owner: str) -> int:
    return int(
        client().eval(
            """
        local phase = redis.call('HGET', KEYS[1], 'phase')
        if not phase or phase == 'done' or phase == 'failed' or phase == 'cancelled' or phase == 'uncertain' then return -1 end
        if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
        local user = redis.call('GET', KEYS[3])
        if user and user ~= ARGV[1] then
            redis.call('HSET', KEYS[1], 'phase', 'cancelled')
            return -1
        end
        if phase == 'sending' then
            redis.call('HSET', KEYS[1], 'phase', 'uncertain')
            if user == ARGV[1] then redis.call('DEL', KEYS[3]) end
            return -1
        end
        redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
        redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[3])
        if phase == 'queued' then redis.call('HSET', KEYS[1], 'phase', 'working') end
        return 1
    """,
            3,
            *keys(request_id, user_id),
            request_id,
            owner,
            LEASE_SECONDS,
        )
    )


def renew(request_id: str, user_id: int, owner: str) -> bool:
    _, execution, user = keys(request_id, user_id)
    return bool(
        client().eval(
            """
        if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        redis.call('EXPIRE', KEYS[2], ARGV[3])
        return 1
    """,
            2,
            execution,
            user,
            owner,
            request_id,
            LEASE_SECONDS,
        )
    )


def transition(
    request_id: str, user_id: int, owner: str, phase: str, sent: list | None = None, total: int | None = None
) -> None:
    payload = json.dumps(sent) if sent is not None else ""
    ok = client().eval(
        """
        if redis.call('GET', KEYS[2]) ~= ARGV[1] or redis.call('GET', KEYS[3]) ~= ARGV[2] then return 0 end
        redis.call('HSET', KEYS[1], 'phase', ARGV[3])
        if ARGV[4] ~= '' then
            redis.call('HSET', KEYS[1], 'sent', ARGV[4], 'offset', ARGV[5])
        end
        if ARGV[7] ~= '' then redis.call('HSET', KEYS[1], 'total', ARGV[7]) end
        redis.call('EXPIRE', KEYS[1], ARGV[6])
        return 1
    """,
        3,
        *keys(request_id, user_id),
        owner,
        request_id,
        phase,
        payload,
        len(sent or []),
        SRC_TTL,
        "" if total is None else total,
    )
    if not ok:
        raise LeaseLost("Request ownership was lost.")


def set_step(request_id: str, user_id: int, owner: str, step: str) -> None:
    """Record how far an inline delivery got (`stored` -> `edited` / `pm_sending` -> `pm` / `start`),
    so a replay never repeats a send that may already have reached Telegram."""
    ok = client().eval(
        """
        if redis.call('GET', KEYS[2]) ~= ARGV[1] or redis.call('GET', KEYS[3]) ~= ARGV[2] then return 0 end
        redis.call('HSET', KEYS[1], 'step', ARGV[3])
        return 1
    """,
        3,
        *keys(request_id, user_id),
        owner,
        request_id,
        step,
    )
    if not ok:
        raise LeaseLost("Request ownership was lost before recording the inline delivery step.")


def release(request_id: str, user_id: int, owner: str) -> None:
    client().eval(
        """
        if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
        redis.call('DEL', KEYS[2])
        local phase = redis.call('HGET', KEYS[1], 'phase')
        if phase == 'queued' then
            if redis.call('GET', KEYS[3]) == ARGV[2] then redis.call('EXPIRE', KEYS[3], ARGV[3]) end
        elseif redis.call('GET', KEYS[3]) == ARGV[2] then
            redis.call('DEL', KEYS[3])
        end
        return 1
    """,
        3,
        *keys(request_id, user_id),
        owner,
        request_id,
        get_settings().queue_ttl + 60,
    )


def cancel_queued(request_id: str, user_id: int, allow_idle: bool = False) -> bool:
    job, execution, user = keys(request_id, user_id)
    return bool(
        client().eval(
            """
        local phase = redis.call('HGET', KEYS[1], 'phase')
        if phase ~= 'queued' then
            if ARGV[2] ~= '1' or (phase ~= 'working' and phase ~= 'ready') or redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
        end
        redis.call('HSET', KEYS[1], 'phase', 'cancelled')
        if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
        return 1
    """,
            3,
            job,
            user,
            execution,
            request_id,
            "1" if allow_idle else "0",
        )
    )


# request_cancel() outcomes
CANCEL_UNKNOWN, CANCEL_STOPPED, CANCEL_SIGNALLED, CANCEL_TOO_LATE = 0, 1, 2, 3


def cancel_key(request_id: str, user_id: int) -> str:
    return keys(request_id, user_id)[0] + ":cancel"


def request_cancel(request_id: str, user_id: int) -> int:
    """The user pressed Cancel. A queued job is cancelled at once (CANCEL_STOPPED); a running one
    gets a flag its worker polls (CANCEL_SIGNALLED). Jobs already sending or finished can't be
    stopped (CANCEL_TOO_LATE). Only the requesting user may cancel; anyone else, or an expired
    request, gets CANCEL_UNKNOWN."""
    job, execution, user = keys(request_id, user_id)
    return int(
        client().eval(
            """
        local phase = redis.call('HGET', KEYS[1], 'phase')
        if not phase or redis.call('HGET', KEYS[1], 'user') ~= ARGV[3] then return 0 end
        if phase == 'queued' then
            redis.call('HSET', KEYS[1], 'phase', 'cancelled')
            if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
            return 1
        end
        if phase == 'working' then
            redis.call('SET', KEYS[3], '1', 'EX', ARGV[2])
            return 2
        end
        return 3
    """,
            3,
            job,
            user,
            cancel_key(request_id, user_id),
            request_id,
            get_settings().task_time_limit + 600,
            user_id,
        )
    )


def cancel_requested(request_id: str, user_id: int) -> bool:
    return bool(client().exists(cancel_key(request_id, user_id)))


@asynccontextmanager
async def execution(request_id: str, user_id: int):
    owner = uuid.uuid4().hex
    acquired = await asyncio.to_thread(claim, request_id, user_id, owner)
    if acquired == 0:
        raise LeaseBusy
    if acquired < 0:
        yield None
        return

    async def heartbeat():
        while True:
            await asyncio.sleep(LEASE_SECONDS // 3)
            try:
                if not await asyncio.to_thread(renew, request_id, user_id, owner):
                    return
            except Exception:
                log.exception("request lease renewal failed: %s", request_id)
                return  # the next transition refuses to send without ownership

    task = asyncio.create_task(heartbeat())
    try:
        yield owner
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        try:
            await asyncio.to_thread(release, request_id, user_id, owner)
        except Exception:
            log.exception("request lease release failed: %s", request_id)
