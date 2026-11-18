"""Process and container health of the bot and worker, published to Redis for /admin.

Each process runs a daemon thread that writes a heartbeat every HEARTBEAT_EVERY seconds (TTL 90 s),
so the admin panel sees every live node, including workers in other containers, without
reaching into them. Memory and CPU come from the container's cgroup (v2) when available.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)
HEARTBEAT_EVERY = 30
CGROUP = Path("/sys/fs/cgroup")
STARTED = time.time()


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None  # e.g. "max" (no limit)


def rss_bytes() -> int | None:
    status = _read(Path("/proc/self/status")) or ""
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def cgroup_memory() -> tuple[int | None, int | None]:
    """(current, limit) of this container's cgroup, or Nones outside cgroup v2."""
    return _int(_read(CGROUP / "memory.current")), _int(_read(CGROUP / "memory.max"))


def cgroup_cpu_usec() -> int | None:
    for line in (_read(CGROUP / "cpu.stat") or "").splitlines():
        if line.startswith("usage_usec "):
            return int(line.split()[1])
    return None


def cpu_quota() -> float | None:
    """CPUs this cgroup may use (cpu.max), None when unlimited."""
    text = _read(CGROUP / "cpu.max")
    if not text:
        return None
    quota, _, period = text.partition(" ")
    if quota == "max" or not period:
        return None
    return int(quota) / int(period)


class CpuMeter:
    """CPU usage of the cgroup (or this process) between two samples, in percent of one core."""

    def __init__(self) -> None:
        self.last: tuple[float, float] | None = None

    @staticmethod
    def _used() -> float:
        usec = cgroup_cpu_usec()
        if usec is not None:
            return usec / 1e6
        times = os.times()
        return times.user + times.system

    def sample(self) -> float | None:
        now, used = time.monotonic(), self._used()
        previous, self.last = self.last, (now, used)
        if previous is None or now <= previous[0]:
            return None
        return max(0.0, (used - previous[1]) / (now - previous[0]) * 100)


def snapshot(role: str, meter: CpuMeter | None = None, disk: Path | None = None) -> dict:
    current, limit = cgroup_memory()
    data = {
        "role": role,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started": STARTED,
        "at": time.time(),
        "rss": rss_bytes(),
        "mem": current,
        "mem_max": limit,
        "cpus": cpu_quota() or os.cpu_count(),
        "cpu": meter.sample() if meter else None,
        "threads": threading.active_count(),
    }
    try:
        data["load"] = list(os.getloadavg())
    except OSError:
        data["load"] = None
    if disk is not None:
        try:
            usage = shutil.disk_usage(disk)
            data["disk"] = {"total": usage.total, "used": usage.used, "free": usage.free}
            from app.services import storage

            data["retained"] = storage.usage(disk)
        except Exception:
            data["disk"] = None
    return data


def node_name(role: str) -> str:
    return f"{role}:{socket.gethostname()}:{os.getpid()}"


def publish(role: str, meter: CpuMeter | None = None, disk: Path | None = None) -> None:
    from app.services import stats

    stats.publish_node(node_name(role), snapshot(role, meter, disk))


def start_heartbeat(role: str, disk: Path | None = None) -> threading.Thread:
    """Publish this process's health now and every HEARTBEAT_EVERY seconds (daemon thread)."""
    meter = CpuMeter()
    meter.sample()

    def loop() -> None:
        while True:
            try:
                publish(role, meter, disk)
            except Exception:
                log.debug("health heartbeat failed", exc_info=True)
            time.sleep(HEARTBEAT_EVERY)

    thread = threading.Thread(target=loop, name=f"{role}-heartbeat", daemon=True)
    thread.start()
    return thread
