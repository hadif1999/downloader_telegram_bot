FROM python:3.12-slim

# ffmpeg: merging streams, mp3 conversion, thumbnails. deno: YouTube JS challenges (yt-dlp EJS).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

RUN useradd --create-home --uid 1000 app && mkdir -p /downloads && chown app:app /downloads
USER app

CMD ["python", "-m", "app.bot.main"]
