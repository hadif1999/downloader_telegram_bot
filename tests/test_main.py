import signal
import subprocess
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

import main as launcher
from app.config import Settings


def test_default_local_environment():
    env = launcher.local_env(Settings(_env_file=None, telegram_api_id="123", telegram_api_hash="test"))
    assert env["REDIS_URL"] == "redis://127.0.0.1:6379/0"
    assert env["BOT_API_URL"] == "http://127.0.0.1:8081"
    assert env["POT_PROVIDER_URL"] == "http://127.0.0.1:4416"
    assert env["BOT_API_LOCAL"] == "false"
    assert env["DOWNLOAD_DIR"] == str(launcher.ROOT / "downloads")
    assert env["COOKIES_DIR"] == str(launcher.ROOT / "cookies")
    assert env["MAX_FILE_MB"] == "2000"


def test_missing_local_api_credentials_uses_cloud():
    env = launcher.local_env(Settings(_env_file=None, telegram_api_id="", telegram_api_hash=""))
    assert env["BOT_API_URL"] == ""
    assert env["MAX_FILE_MB"] == "50"


def test_deno_install_directory_is_added_to_child_path(monkeypatch, tmp_path):
    deno_dir = tmp_path / ".deno" / "bin"
    deno_dir.mkdir(parents=True)
    (deno_dir / "deno").touch()
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    env = launcher.local_env(Settings(_env_file=None))
    assert env["PATH"].split(launcher.os.pathsep)[0] == str(deno_dir)


def test_custom_local_environment(tmp_path):
    settings = Settings(
        _env_file=None,
        redis_url="redis://other-host:6380/1",
        bot_api_url="",
        pot_provider_url="",
        download_dir=tmp_path,
        cookies_dir=tmp_path / "auth",
    )
    env = launcher.local_env(settings)
    assert env["REDIS_URL"] == settings.redis_url
    assert env["BOT_API_URL"] == env["POT_PROVIDER_URL"] == ""
    assert env["DOWNLOAD_DIR"] == str(tmp_path)
    assert env["COOKIES_DIR"] == str(tmp_path / "auth")


def test_wait_for_services_retries(monkeypatch):
    connect = MagicMock(side_effect=[OSError(), MagicMock()])
    monkeypatch.setattr(launcher.socket, "create_connection", connect)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launcher.wait_for_services({"REDIS_URL": "redis://localhost", "BOT_API_URL": "", "POT_PROVIDER_URL": ""})
    assert connect.call_count == 2
    connect.assert_called_with(("localhost", 6379), timeout=1)


def test_service_timeout(monkeypatch):
    monkeypatch.setattr(launcher.socket, "create_connection", MagicMock(side_effect=OSError()))
    monkeypatch.setattr(launcher.time, "monotonic", MagicMock(side_effect=[0, 61]))
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        launcher.wait_for_services(
            {"REDIS_URL": "redis://localhost", "BOT_API_URL": "", "POT_PROVIDER_URL": ""}
        )


def test_http_readiness_requires_http_response(monkeypatch):
    request = MagicMock(
        side_effect=[ConnectionResetError(), HTTPError("http://localhost", 404, "", {}, None)]
    )
    monkeypatch.setattr(launcher, "urlopen", request)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launcher.wait_for_services(
        {"REDIS_URL": "", "BOT_API_URL": "http://localhost:8081", "POT_PROVIDER_URL": ""}
    )
    assert request.call_count == 2


def test_http_server_errors_are_not_ready(monkeypatch):
    request = MagicMock(side_effect=HTTPError("http://localhost", 503, "", {}, None))
    monkeypatch.setattr(launcher, "urlopen", request)
    monkeypatch.setattr(launcher.time, "monotonic", MagicMock(side_effect=[0, 61]))
    with pytest.raises(RuntimeError, match="BOT_API_URL"):
        launcher.wait_for_services(
            {"REDIS_URL": "", "BOT_API_URL": "http://localhost:8081", "POT_PROVIDER_URL": ""}
        )


@pytest.fixture
def local_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher.os, "chdir", lambda _: None)
    monkeypatch.setattr(launcher, "Settings", lambda: Settings(_env_file=None, bot_token="test-token"))
    monkeypatch.setattr(launcher.shutil, "which", lambda name, **kwargs: name)
    compose = MagicMock()
    monkeypatch.setattr(launcher.subprocess, "run", compose)
    monkeypatch.setattr(launcher, "wait_for_services", lambda _: None)
    kill = MagicMock()
    monkeypatch.setattr(launcher.os, "killpg", kill)
    return compose, kill


def test_starts_both_processes_and_stops_peer_on_exit(monkeypatch, local_startup):
    compose, kill = local_startup
    worker = MagicMock(pid=100)
    worker.poll.return_value = 7
    bot = MagicMock(pid=101)
    bot.poll.return_value = None
    popen = MagicMock(side_effect=[worker, bot])
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.main() == 7
    assert popen.call_args_list[0].args[0][1:3] == ["-m", "celery"]
    assert popen.call_args_list[1].args[0][1:] == ["-m", "app.bot.main"]
    assert popen.call_args.kwargs["env"]["BOT_API_LOCAL"] == "false"
    assert compose.call_args.kwargs["check"] is True
    assert "telegram-bot-api" not in compose.call_args.args[0]
    kill.assert_called_once_with(101, signal.SIGTERM)


def test_failed_second_process_cleans_up_worker(monkeypatch, local_startup):
    _, kill = local_startup
    worker = MagicMock(pid=100)
    worker.poll.return_value = None
    monkeypatch.setattr(launcher.subprocess, "Popen", MagicMock(side_effect=[worker, OSError("failed")]))
    with pytest.raises(OSError, match="failed"):
        launcher.main()
    kill.assert_called_once_with(100, signal.SIGTERM)
    worker.wait.assert_called_once_with(timeout=20)


def test_ctrl_c_stops_both_processes(monkeypatch, local_startup):
    _, kill = local_startup
    children = [MagicMock(pid=100), MagicMock(pid=101)]
    for child in children:
        child.poll.return_value = None
    children[0].wait.side_effect = [subprocess.TimeoutExpired("worker", 20), 0]
    monkeypatch.setattr(launcher.subprocess, "Popen", MagicMock(side_effect=children))
    monkeypatch.setattr(launcher.time, "sleep", MagicMock(side_effect=KeyboardInterrupt()))
    assert launcher.main() == 0
    assert (100, signal.SIGKILL) in [call.args for call in kill.call_args_list]
    assert (101, signal.SIGTERM) in [call.args for call in kill.call_args_list]


def test_missing_bot_token_fails_before_starting_services(monkeypatch, local_startup):
    compose, _ = local_startup
    monkeypatch.setattr(launcher, "Settings", lambda: Settings(_env_file=None, bot_token=""))
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        launcher.main()
    compose.assert_not_called()
