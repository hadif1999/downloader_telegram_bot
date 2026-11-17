"""Bounded on-disk media retention and download-directory housekeeping.

Every download runs in a private attempt directory ``DOWNLOAD_DIR/<uuid hex>`` that the worker removes
when it finishes. The finished files are additionally hardlinked into ``DOWNLOAD_DIR/.media-cache``
so that a retry of the same platform/media/format/language (e.g. after a failed or uncertain upload)
is served from disk instead of being downloaded again. Hardlinks cost no extra space while both
names exist.

The retained set is bounded three ways: at most ``MEDIA_CACHE_ITEMS`` entries (least recently used
evicted first), at most ``MEDIA_CACHE_MAX_MB`` in total, and no entry older than ``MEDIA_CACHE_TTL``
since its last use. Before a new download the worker also keeps ``MIN_FREE_DISK_MB`` free, evicting
retained media if necessary and refusing the download when the disk is still too full.

Multiple worker processes (or containers sharing the volume) coordinate with ``flock``:

* ``.lock`` serialises cache structure changes (lookup + pin, store, prune).
* ``<key>.pin`` is held with a shared lock while an entry is being read or uploaded. Eviction only
  removes an entry whose pin it can lock exclusively without waiting, so in-use files never vanish.

Retention is an optimisation: any failure to store or read the cache falls back to the normal
download path and never turns a delivery into a failure.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.models import DownloadResult
from app.platforms.ytdlp import UserError

log = logging.getLogger(__name__)

CACHE_DIR = ".media-cache"
MANIFEST = "manifest.json"
VERSION = 1
ATTEMPT_RE = re.compile(r"[0-9a-f]{32}")
KEY_RE = re.compile(r"[0-9a-f]{40}")
LOW_STORAGE = "The server is low on storage right now. Please try again in a few minutes."


class StorageFull(UserError):
    """Not enough free disk space for a new download, even after evicting retained media."""


@dataclass
class _Entry:
    key: str
    path: Path
    used: float
    size: int
    valid: bool


@dataclass
class Slot:
    """A pinned retention slot for one platform/media/format/language combination."""

    root: Path
    key: str
    result: DownloadResult | None = None
    _pin: int | None = field(default=None, repr=False)

    def store(self, result: DownloadResult) -> DownloadResult:
        """Retain `result`'s files and return a result pointing at the retained copies.

        Falls back to the original `result` when retention is disabled, the files don't fit the
        limits, or linking fails (e.g. the cache would be on another filesystem)."""
        try:
            return _store(self.root, self.key, result)
        except Exception:
            log.exception("couldn't retain downloaded media: key=%s", self.key)
            return result

    def close(self) -> None:
        if self._pin is not None:
            with contextlib.suppress(OSError):
                os.close(self._pin)  # releases the shared lock
            self._pin = None
        try:
            prune(self.root)
        except Exception:
            log.exception("media cache pruning failed")


# ---------------------------------------------------------------- public API


def entry_key(platform: str, media_id: str, fmt: str, lang: str | None) -> str:
    return hashlib.sha1(f"{platform}\0{media_id}\0{fmt}\0{lang or ''}".encode()).hexdigest()


def open_slot(root: Path, platform: str, media_id: str, fmt: str, lang: str | None) -> Slot:
    """Pin the slot and return any valid retained result. Always `close()` the returned slot."""
    key = entry_key(platform, media_id, fmt, lang)
    slot = Slot(root=root, key=key)
    if get_settings().media_cache_items <= 0:
        return slot
    try:
        cache = _cache_dir(root)
        with _global_lock(cache):
            # Pins are only taken under the global lock, so prune can't race a half-taken pin.
            slot._pin = os.open(cache / f"{key}.pin", os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(slot._pin, fcntl.LOCK_SH)
            slot.result = _load(cache / key)
            if slot.result is not None:
                with contextlib.suppress(OSError):
                    os.utime(cache / key / MANIFEST)  # mark as most recently used
    except Exception:
        log.exception("media cache lookup failed: key=%s", key)
        slot.result = None
    return slot


def ensure_space(root: Path, needed: int = 0) -> None:
    """Keep MIN_FREE_DISK_MB free after a download of about `needed` bytes, or raise StorageFull."""
    s = get_settings()
    reserve = s.min_free_disk_mb * 1024 * 1024
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free >= needed + reserve:
        return
    prune(root, keep=0)  # release every retained file that isn't being uploaded right now
    free = shutil.disk_usage(root).free
    if free < needed + reserve:
        log.warning("refusing download: free=%d needed=%d reserve=%d", free, needed, reserve)
        raise StorageFull(LOW_STORAGE)


def prune(root: Path, keep: int | None = None) -> None:
    """Enforce the retention limits and remove leftovers of crashed attempts."""
    s = get_settings()
    keep = s.media_cache_items if keep is None else keep
    max_bytes = s.media_cache_max_mb * 1024 * 1024
    now = time.time()
    # Attempts can't outlive the Celery hard time limit; older directories belong to killed workers.
    stale_after = s.task_time_limit + 600
    _sweep_attempts(root, now - stale_after)
    cache = root / CACHE_DIR
    if not cache.is_dir():
        return
    with _global_lock(cache):
        _sweep_tmp(cache, now - stale_after)
        entries = sorted(_entries(cache), key=lambda e: e.used, reverse=True)
        kept = total = 0
        for entry in entries:
            fits = (
                entry.valid
                and kept < keep
                and total + entry.size <= max_bytes
                and now - entry.used <= s.media_cache_ttl
            )
            if fits:
                kept += 1
                total += entry.size
            elif not _evict(cache, entry.key):
                kept += 1  # pinned by an active upload: keep it and count it against the limit
                total += entry.size
        _sweep_pins(cache, {e.key for e in entries})


def usage(root: Path) -> dict[str, int]:
    """Retained entry count and bytes, for logs and diagnostics."""
    cache = root / CACHE_DIR
    entries = list(_entries(cache)) if cache.is_dir() else []
    return {"items": len(entries), "bytes": sum(e.size for e in entries)}


# ---------------------------------------------------------------- internals


def _cache_dir(root: Path) -> Path:
    cache = root / CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    return cache


@contextlib.contextmanager
def _global_lock(cache: Path):
    fd = os.open(cache / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _store(root: Path, key: str, result: DownloadResult) -> DownloadResult:
    s = get_settings()
    if s.media_cache_items <= 0:
        return result
    sources = list(result.files) if result.files else [(result.path, "")]
    sizes = [Path(p).stat().st_size for p, _ in sources]
    if sum(sizes) > s.media_cache_max_mb * 1024 * 1024:
        return result
    cache = _cache_dir(root)
    tmp = cache / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        names = []
        for index, (path, _) in enumerate(sources):
            name = f"{index:03d}{Path(path).suffix.lower()[:10]}"
            os.link(path, tmp / name)  # same volume: no extra disk while the attempt dir exists
            names.append(name)
        manifest = {
            "version": VERSION,
            "single": not result.files,
            "files": [[n, kind, size] for n, (_, kind), size in zip(names, sources, sizes, strict=True)],
            "width": result.width,
            "height": result.height,
            "duration": result.duration,
            "title": result.title,
            "performer": result.performer,
        }
        (tmp / MANIFEST).write_text(json.dumps(manifest))
        target = cache / key
        with _global_lock(cache):
            existing = _load(target)
            if existing is not None:
                return existing  # a concurrent request for the same media stored it first
            shutil.rmtree(target, ignore_errors=True)  # invalid leftovers only
            os.rename(tmp, target)
        return _load(target) or result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _load(entry: Path) -> DownloadResult | None:
    try:
        manifest = json.loads((entry / MANIFEST).read_text())
        if manifest.get("version") != VERSION:
            return None
        if time.time() - (entry / MANIFEST).stat().st_mtime > get_settings().media_cache_ttl:
            return None
        files = []
        for name, kind, size in manifest["files"]:
            path = entry / name
            if path.parent != entry or path.stat().st_size != size:
                return None
            files.append((str(path), kind))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not files:
        return None
    extra = {k: manifest.get(k) for k in ("width", "height", "duration", "title", "performer")}
    if manifest.get("single"):
        return DownloadResult(path=files[0][0], **extra)
    return DownloadResult(files=files, **extra)


def _entries(cache: Path):
    for path in cache.iterdir():
        if not (KEY_RE.fullmatch(path.name) and path.is_dir()):
            continue
        try:
            used = (path / MANIFEST).stat().st_mtime
            valid = True
        except OSError:
            used, valid = 0.0, False
        size = 0
        for file in path.iterdir():
            with contextlib.suppress(OSError):
                size += file.stat().st_size
        yield _Entry(path.name, path, used, size, valid and _load(path) is not None)


def _evict(cache: Path, key: str) -> bool:
    """Remove an entry unless an upload holds its pin. Caller holds the global lock."""
    pin = cache / f"{key}.pin"
    fd = os.open(pin, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        shutil.rmtree(cache / key, ignore_errors=True)
        pin.unlink(missing_ok=True)
        log.info("evicted retained media: key=%s", key)
        return True
    finally:
        os.close(fd)


def _sweep_pins(cache: Path, live: set[str]) -> None:
    for pin in cache.glob("*.pin"):
        if pin.stem in live or not KEY_RE.fullmatch(pin.stem):
            continue
        if (cache / pin.stem).exists():
            continue
        fd = os.open(pin, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pin.unlink(missing_ok=True)
        except BlockingIOError:
            pass
        finally:
            os.close(fd)


def _sweep_tmp(cache: Path, cutoff: float) -> None:
    for tmp in cache.glob(".tmp-*"):
        with contextlib.suppress(OSError):
            if tmp.stat().st_mtime < cutoff:
                shutil.rmtree(tmp, ignore_errors=True)


def _sweep_attempts(root: Path, cutoff: float) -> None:
    if not root.is_dir():
        return
    for path in root.iterdir():
        if not ATTEMPT_RE.fullmatch(path.name):
            continue
        with contextlib.suppress(OSError):
            if path.is_dir() and _newest_mtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                log.warning("removed orphaned download directory: %s", path.name)


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        with contextlib.suppress(OSError):
            newest = max(newest, child.stat().st_mtime)
    return newest
