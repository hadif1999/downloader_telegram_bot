import os
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.models import DownloadResult
from app.services import storage


@pytest.fixture
def limits(monkeypatch):
    s = NS(
        media_cache_items=3,
        media_cache_max_mb=1,
        media_cache_ttl=3600,
        min_free_disk_mb=1,
        task_time_limit=120,
    )
    monkeypatch.setattr(storage, "get_settings", lambda: s)
    return s


def download(root, name, data=b"media", kind=None):
    attempt = root / os.urandom(16).hex()
    attempt.mkdir()
    path = attempt / f"{name}.mp4"
    path.write_bytes(data)
    if kind:
        return attempt, DownloadResult(files=[(str(path), kind)])
    return attempt, DownloadResult(path=str(path), width=1280, height=720, duration=3)


def deliver(root, media_id, data=b"media"):
    """What the worker does: pin, store, upload, drop the attempt directory, release."""
    slot = storage.open_slot(root, "yt", media_id, "720", None)
    attempt, result = download(root, media_id, data)
    stored = slot.store(result)
    for path in attempt.iterdir():
        path.unlink()
    attempt.rmdir()
    slot.close()
    return stored


def retained(root, media_id):
    slot = storage.open_slot(root, "yt", media_id, "720", None)
    try:
        return slot.result
    finally:
        slot.close()


def test_keeps_only_three_most_recent_downloads(tmp_path, limits):
    for index in range(5):
        deliver(tmp_path, f"m{index}")
        time.sleep(0.01)  # distinct last-used times
    assert storage.usage(tmp_path)["items"] == 3
    assert retained(tmp_path, "m0") is None and retained(tmp_path, "m1") is None
    hit = retained(tmp_path, "m4")
    assert hit is not None and Path(hit.path).read_bytes() == b"media"
    assert (hit.width, hit.height, hit.duration) == (1280, 720, 3)


def test_reuse_refreshes_recency(tmp_path, limits):
    for index in range(3):
        deliver(tmp_path, f"m{index}")
        time.sleep(0.01)
    assert retained(tmp_path, "m0") is not None  # m0 becomes most recently used
    time.sleep(0.01)
    deliver(tmp_path, "m3")
    assert retained(tmp_path, "m0") is not None
    assert retained(tmp_path, "m1") is None


def test_retention_hardlinks_instead_of_copying(tmp_path, limits):
    slot = storage.open_slot(tmp_path, "yt", "m", "720", None)
    _, result = download(tmp_path, "m")
    stored = slot.store(result)
    assert os.stat(stored.path).st_ino == os.stat(result.path).st_ino
    slot.close()


def test_pinned_entry_is_not_evicted_during_upload(tmp_path, limits):
    deliver(tmp_path, "uploading")
    pinned = storage.open_slot(tmp_path, "yt", "uploading", "720", None)
    assert pinned.result is not None
    for index in range(4):
        time.sleep(0.01)
        deliver(tmp_path, f"m{index}")
    assert os.path.exists(pinned.result.path)
    pinned.close()  # released: now the oldest entry is evicted
    assert retained(tmp_path, "uploading") is None
    assert storage.usage(tmp_path)["items"] == 3


def test_files_over_size_cap_are_not_retained(tmp_path, limits):
    stored = deliver(tmp_path, "huge", data=b"x" * (2 * 1024 * 1024))
    assert storage.CACHE_DIR not in stored.path
    assert storage.usage(tmp_path)["items"] == 0


def test_expired_entries_are_missed_and_pruned(tmp_path, limits):
    deliver(tmp_path, "old")
    manifest = next((tmp_path / storage.CACHE_DIR).glob("*/manifest.json"))
    past = time.time() - 7200
    os.utime(manifest, (past, past))
    assert retained(tmp_path, "old") is None
    assert storage.usage(tmp_path)["items"] == 0


def test_corrupt_entry_is_replaced(tmp_path, limits):
    deliver(tmp_path, "m")
    manifest = next((tmp_path / storage.CACHE_DIR).glob("*/manifest.json"))
    manifest.write_text("{not json")
    assert retained(tmp_path, "m") is None
    stored = deliver(tmp_path, "m", data=b"fresh")
    assert Path(stored.path).read_bytes() == b"fresh"
    assert retained(tmp_path, "m") is not None


def test_gallery_results_keep_order_and_kinds(tmp_path, limits):
    slot = storage.open_slot(tmp_path, "yt", "album", "720", None)
    attempt = tmp_path / os.urandom(16).hex()
    attempt.mkdir()
    files = []
    for index, kind in enumerate(["photo", "video", "photo"]):
        path = attempt / f"item{index}.{'jpg' if kind == 'photo' else 'mp4'}"
        path.write_bytes(bytes([index]))
        files.append((str(path), kind))
    slot.store(DownloadResult(files=files))
    slot.close()
    hit = retained(tmp_path, "album")
    assert hit is not None
    assert [kind for _, kind in hit.files] == ["photo", "video", "photo"]
    assert [Path(p).read_bytes() for p, _ in hit.files] == [b"\0", b"\1", b"\2"]


def test_disabled_retention_keeps_nothing(tmp_path, limits):
    limits.media_cache_items = 0
    stored = deliver(tmp_path, "m")
    assert storage.CACHE_DIR not in stored.path
    assert not (tmp_path / storage.CACHE_DIR).exists()


def test_orphaned_attempt_directories_are_swept(tmp_path, limits):
    stale, _ = download(tmp_path, "stale")
    fresh, _ = download(tmp_path, "fresh")
    other = tmp_path / "not-an-attempt"
    other.mkdir()
    past = time.time() - 3600
    for path in [stale, *stale.iterdir()]:
        os.utime(path, (past, past))
    storage.prune(tmp_path)
    assert not stale.exists()
    assert fresh.exists() and other.exists()


def test_low_disk_evicts_retained_media_before_downloading(tmp_path, limits, monkeypatch):
    deliver(tmp_path, "m")
    free = iter([0, 10 * 1024 * 1024])
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: NS(free=next(free)))
    storage.ensure_space(tmp_path, needed=1024)
    assert storage.usage(tmp_path)["items"] == 0


def test_low_disk_refuses_when_eviction_is_not_enough(tmp_path, limits, monkeypatch):
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: NS(free=0))
    with pytest.raises(storage.StorageFull):
        storage.ensure_space(tmp_path, needed=1024)
