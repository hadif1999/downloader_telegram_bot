"""Calendar buckets for statistics: day/hour keys in the configured STATS_TIMEZONE."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import lru_cache

OFFSET = re.compile(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?")


@lru_cache(maxsize=8)
def parse_timezone(name: str) -> tzinfo:
    """An IANA zone name, "UTC", or a fixed offset like "+03:30" / "UTC-5"."""
    text = (name or "UTC").strip()
    if text.upper() in {"UTC", "GMT", "Z"}:
        return UTC
    if match := OFFSET.fullmatch(text):
        sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f"STATS_TIMEZONE offset out of range: {name!r}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta, text)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception:
        raise ValueError(
            f"STATS_TIMEZONE {name!r} is not a known time zone; use a fixed offset such as +03:30"
        ) from None


def zone() -> tzinfo:
    from app.config import get_settings

    return parse_timezone(get_settings().stats_timezone)


def local(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else ts, zone())


def day_key(ts: float | None = None) -> str:
    return local(ts).strftime("%Y%m%d")


def hour_key(ts: float | None = None) -> str:
    return local(ts).strftime("%Y%m%d%H")


def days_back(count: int, ts: float | None = None) -> list[datetime]:
    """The last `count` local days, oldest first, ending today."""
    today = local(ts).replace(hour=12, minute=0, second=0, microsecond=0)  # noon: DST-safe steps
    return [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def hours_back(count: int, ts: float | None = None) -> list[datetime]:
    """The last `count` local hours, oldest first, ending with the current hour."""
    now = time.time() if ts is None else ts
    start = int(now // 3600) * 3600
    return [local(start - 3600 * offset) for offset in range(count - 1, -1, -1)]


def start_of_day(ts: float | None = None) -> float:
    moment = local(ts)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
