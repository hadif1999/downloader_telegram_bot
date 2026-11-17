"""Deterministic offline tests: never read deployment credentials or contact websites."""

import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest
import redis

from app import config

config.Settings.model_config["env_file"] = None
os.environ["BOT_TOKEN"] = "123456:offline-tests"
os.environ["DEFAULT_LANGUAGE"] = "en"  # assertions read English; Farsi is tested explicitly
os.environ.pop("FORCE_JOIN_CHANNEL_ID", None)
os.environ.pop("FORCE_JOIN_CHANNEL_URL", None)
for name in (
    "YOUTUBE",
    "INSTAGRAM",
    "TIKTOK",
    "PINTEREST",
    "SNAPCHAT",
    "LIKEE",
    "VK",
    "FACEBOOK",
    "THREADS",
    "SOUNDCLOUD",
    "SPOTIFY",
):
    os.environ[f"{name}_ENABLED"] = "true"
config.get_settings.cache_clear()

# Some fixtures build metadata during collection. Keep those thumbnail probes offline too.
from app.platforms import youtube  # noqa: E402 -- configure offline settings before registry imports

youtube._url_ok = lambda _: False


@pytest.fixture(autouse=True)
def interface_language():
    """Each test starts in the default language with no remembered user choices."""
    from app import i18n

    token = i18n._current.set(None)
    i18n._cache.clear()
    yield
    i18n._current.reset(token)
    i18n._cache.clear()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family == socket.AF_UNIX:
            return connect(sock, address)
        raise AssertionError("External networking is disabled in tests")

    def blocked_dns(*args, **kwargs):
        raise AssertionError("DNS is disabled in tests; mock the network boundary")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    monkeypatch.setattr(youtube, "_url_ok", lambda _: False)


@pytest.fixture(scope="session")
def redis_server():
    """Use the already-installed image, no network, exposed ports or deployment state."""
    if not shutil.which("docker"):
        pytest.skip("Docker is required for isolated Redis Lua checks")
    with tempfile.TemporaryDirectory(prefix="media-redis-test-") as directory:
        command = [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={directory},dst=/redis",
            "redis:7-alpine",
            "redis-server",
            "--port",
            "0",
            "--save",
            "",
            "--appendonly",
            "no",
            "--unixsocket",
            "/redis/socket",
        ]
        started = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if started.returncode:
            pytest.skip("Isolated Redis image unavailable (no image is pulled by tests)")
        container = started.stdout.strip()
        client = redis.Redis(unix_socket_path=f"{directory}/socket", decode_responses=True, socket_timeout=2)
        try:
            for attempt in range(3):
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.3)
            yield client
        finally:
            client.close()
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=10)


@pytest.fixture
def request_store(redis_server, monkeypatch):
    from app.services import cache, requests

    redis_server.flushdb()  # only the ephemeral, network-isolated instance above
    monkeypatch.setattr(cache, "client", lambda: redis_server)
    monkeypatch.setattr(requests, "client", lambda: redis_server)
    return redis_server
