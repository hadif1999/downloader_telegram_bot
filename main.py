"""Run local infrastructure, the Telegram bot, and its download worker."""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings

ROOT = Path(__file__).resolve().parent


def local_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    for key, value, docker_host in (
        ("REDIS_URL", settings.redis_url, "redis"),
        ("BOT_API_URL", settings.bot_api_url, "telegram-bot-api"),
        ("POT_PROVIDER_URL", settings.pot_provider_url, "bgutil-provider"),
    ):
        parsed = urlsplit(value)
        if parsed.hostname == docker_host:
            value = value.replace(f"//{docker_host}:", "//127.0.0.1:", 1)
        env[key] = value
    # Host files are uploaded over HTTP; Docker's /downloads volume is not shared with the host.
    env["BOT_API_LOCAL"] = "false"
    for key, value, container_path, local_path in (
        ("DOWNLOAD_DIR", settings.download_dir, "/downloads", "downloads"),
        ("COOKIES_DIR", settings.cookies_dir, "/cookies", "cookies"),
    ):
        env[key] = str(ROOT / local_path if str(value) == container_path else value.resolve())
    return env


def wait_for_services(env: dict[str, str]) -> None:
    deadline = time.monotonic() + 60
    for key in ("REDIS_URL", "BOT_API_URL", "POT_PROVIDER_URL"):
        if not env[key]:
            continue
        url = urlsplit(env[key])
        port = url.port or {"redis": 6379, "rediss": 6379, "http": 80, "https": 443}[url.scheme]
        while True:
            try:
                with socket.create_connection((url.hostname, port), timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{key} service did not become reachable within 60 seconds") from None
                time.sleep(0.5)


def main() -> int:
    os.chdir(ROOT)
    settings = Settings()
    if not settings.bot_token or settings.bot_token == "123456:ABC-your-token-from-BotFather":
        raise RuntimeError("Set BOT_TOKEN in .env before running the bot.")
    for command in ("docker", "ffmpeg", "deno"):
        if not shutil.which(command):
            raise RuntimeError(f"Install {command} before running locally (see README.md).")
    env = local_env(settings)
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.local.yml",
            "up",
            "-d",
            "redis",
            "telegram-bot-api",
            "bgutil-provider",
        ],
        check=True,
    )
    wait_for_services(env)
    Path(env["DOWNLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    commands = [
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.worker.celery_app",
            "worker",
            "--loglevel=INFO",
            f"--concurrency={settings.max_parallel_downloads}",
        ],
        [sys.executable, "-m", "app.bot.main"],
    ]
    children = []
    try:
        for command in commands:
            children.append(subprocess.Popen(command, env=env, start_new_session=True))
        print("Bot and worker running. Press Ctrl+C to stop. Docker services stay running.", flush=True)
        while True:
            for child in children:
                if (code := child.poll()) is not None:
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == "__main__":

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)
