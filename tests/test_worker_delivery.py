import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError

from app.models import DownloadResult, FormatOption, MediaInfo
from app.services import cache, captions, requests
from app.worker import tasks


@pytest.fixture
def worker(request_store, monkeypatch, tmp_path):
    b = MagicMock()
    b.get_me = AsyncMock(return_value=NS(username="test_bot"))
    b.session.close = AsyncMock()
    b.delete_message = AsyncMock()
    b.send_video = AsyncMock(return_value=NS(photo=[], video=NS(file_id="video-id"), audio=None))
    i = MediaInfo(
        "yt", "id", "https://youtu.be/abcdefghijk", "Title", formats=[FormatOption("720", "720p", 10, 720)]
    )
    monkeypatch.setattr(tasks, "make_bot", lambda **kwargs: b)
    monkeypatch.setattr(tasks, "get_settings", lambda: NS(download_dir=tmp_path, max_file_bytes=1000))
    monkeypatch.setattr(tasks.media, "resolve", lambda *a: i)
    monkeypatch.setattr(tasks, "make_thumb", lambda *a: None)
    edit = AsyncMock()
    monkeypatch.setattr(tasks, "edit_status", edit)

    def download(info, fmt, lang, dest, progress):
        p = dest / "media.mp4"
        p.write_bytes(b"video")
        return DownloadResult(path=str(p))

    download = MagicMock(side_effect=download)
    monkeypatch.setattr(tasks.registry, "get", lambda *a: NS(download=download))
    assert requests.create("a", 10)
    return NS(bot=b, info=i, edit=edit, download=download, directory=tmp_path)


def run(is_caption=False):
    return asyncio.run(tasks._run(1, 20, is_caption, "yt", "id", "720", None, None, 10, "a"))


@pytest.mark.parametrize("is_caption", [False, True])
def test_status_layout_is_consistent_for_text_and_photo_cards(worker, is_caption):
    worker.info.uploader = "Creator"
    run(is_caption)
    updates = worker.edit.call_args_list
    assert updates[0].args[3].startswith("⏳ <b>Preparing download</b>")
    assert updates[-1].args[3].startswith("📤 <b>Uploading to Telegram</b>")
    assert all("Title" in c.args[3] and "Creator · 720p" in c.args[3] for c in updates)
    assert all(c.args[4] == is_caption for c in updates)


def test_success_caches_result_cleans_files_and_deduplicates_replay(worker):
    run()
    run()
    assert worker.download.call_count == 1
    worker.bot.send_video.assert_awaited_once()
    assert cache.get_file_id("yt", "id", "720", None) == "video-id"
    assert requests.load("a", 10)["phase"] == "done"
    assert not list(worker.directory.iterdir())


def test_file_cache_failure_does_not_turn_success_into_failure(worker, monkeypatch):
    monkeypatch.setattr(cache, "save_file_id", MagicMock(side_effect=ConnectionError("cache failed")))
    run()
    assert requests.load("a", 10)["phase"] == "done"
    assert all(captions.FAILED not in c.args[3] for c in worker.edit.call_args_list)
    worker.bot.delete_message.assert_awaited_once_with(1, 20)


def test_checkpoint_failure_after_upload_does_not_report_failure_or_repeat(
    worker, monkeypatch, request_store
):
    original = requests.transition

    def transition(request_id, user, owner, phase, *args):
        if phase in {"ready", "done"}:
            raise ConnectionError("Redis unavailable after accepted upload")
        return original(request_id, user, owner, phase, *args)

    monkeypatch.setattr(requests, "transition", transition)
    run()
    assert requests.load("a", 10)["phase"] == "sending"
    assert all(captions.FAILED not in c.args[3] for c in worker.edit.call_args_list)
    run()
    worker.bot.send_video.assert_awaited_once()
    assert requests.load("a", 10)["phase"] == "uncertain"


def test_ambiguous_upload_marks_uncertain_and_raises_not_celery_success(worker):
    worker.bot.send_video.side_effect = TelegramNetworkError(method=MagicMock(), message="response lost")
    with pytest.raises(requests.DeliveryUncertain):
        run()
    assert requests.load("a", 10)["phase"] == "uncertain"
    assert worker.edit.call_args.args[3] == captions.status_caption(worker.info, captions.UNCERTAIN, "720")
    worker.bot.send_video.assert_awaited_once()
    assert not list(worker.directory.iterdir())


def test_download_failure_marks_failed_and_releases_user(worker, request_store):
    worker.download.side_effect = RuntimeError("download failed")
    with pytest.raises(RuntimeError):
        run()
    assert requests.load("a", 10)["phase"] == "failed"
    assert request_store.get(requests.keys("a", 10)[2]) is None
    assert not list(worker.directory.iterdir())


def test_stale_format_is_rejected_before_downloading(worker):
    worker.info.formats.clear()
    with pytest.raises(Exception, match="no longer available"):
        run()
    worker.download.assert_not_called()


def test_replay_after_final_checkpoint_skips_download(worker):
    requests.claim("a", 10, "old")
    requests.transition("a", 10, "old", "ready", [["video", "fid"]], 1)
    requests.release("a", 10, "old")
    run()
    worker.download.assert_not_called()
    worker.bot.send_video.assert_not_awaited()
    assert requests.load("a", 10)["phase"] == "done"


def test_request_store_failure_before_claim_reports_status_and_closes_session(worker, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.services.errors import TEMPORARY

    failure = RedisConnectionError("lease store unavailable")
    monkeypatch.setattr(requests, "claim", MagicMock(side_effect=failure))
    with pytest.raises(RedisConnectionError) as caught:
        run()
    assert caught.value is failure
    assert worker.edit.call_args.args[3] == TEMPORARY
    worker.bot.session.close.assert_awaited_once()
    worker.download.assert_not_called()


@pytest.mark.parametrize("download_fails", [False, True])
def test_session_cleanup_cannot_change_download_outcome(worker, download_fails):
    worker.bot.session.close.side_effect = RuntimeError("cleanup failure")
    if download_fails:
        failure = RuntimeError("original download failure")
        worker.download.side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            run()
        assert caught.value is failure
    else:
        run()
        worker.bot.send_video.assert_awaited_once()
        assert requests.load("a", 10)["phase"] == "done"
