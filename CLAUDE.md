# media-downloader-bot: notes for agents

Telegram bot that downloads media from YouTube, Instagram, TikTok, Pinterest, Threads, SoundCloud,
Spotify (and more, toggled in `.env`). aiogram 3 bot + Celery worker + Redis + local Bot API server,
downloads via yt-dlp / gallery-dl / plain HTTP, ffmpeg for merging and MP3.

Read first: `README.md` (features, config, layout), `docs/RELIABILITY.md` (request journal, leases,
retries, cancel, disk retention: the invariants the tests protect), `docs/DEPLOYMENT.md`
(production runbook), `docs/TODO.md`. Host-specific details: `CLAUDE.local.md` (gitignored).

## Workflow

- Work on branch `dev`, commit with long descriptive messages, never merge to `main` yourself.
- `uv sync` once; then `.venv/bin/pytest -q` (offline: network is blocked in tests, Redis runs from a
  local image), `.venv/bin/ruff check . && .venv/bin/ruff format .`.
  Also run `AIOGRAM_EAGER_MODELS=1 .venv/bin/pytest -q` after touching `app/lean_models.py`.
- `.env` is the production config: keep it mode 600, never print or commit token values.
- Local stack: `docker compose up -d`. Never run it while production is up (same bot token).

## Map

| Area | Where |
|---|---|
| Bot entry, middlewares order (language -> membership gate) | `app/bot/main.py` |
| Link -> format card, callbacks, Cancel button, `/language` | `app/bot/handlers/`, `app/bot/language.py` |
| Required channels (`FORCE_JOIN_CHANNEL_ID`, comma list) | `app/bot/membership.py` |
| Queueing, cached re-sends | `app/services/jobs.py` |
| Playlists / albums / several links: batch store, summary card, chain | `app/services/batches.py`, `app/bot/handlers/batch.py`, `advance_batch` in `app/worker/tasks.py` |
| Celery task: download, live progress, retries, upload | `app/worker/tasks.py` |
| Progress card rendering (thread-safe Monitor) | `app/services/progress.py` |
| Request state machine in Redis (Lua), cancel flags | `app/services/requests.py` |
| Disk retention (last 3 media, free-space guard) | `app/services/storage.py` |
| Segmented downloads: parallel connections, work stealing, adaptive request size, resume table | `app/platforms/segmented.py` |
| `download_file`, single-stream fallback, stall timeout, background writeback | `app/platforms/common.py` |
| yt-dlp hook: `SegmentedHttpFD` for HTTP formats, retry hook, guarded transport | `app/platforms/downloader.py` |
| Stream cache (worker reuses the bot's extraction, 2 h) | `app/platforms/ytdlp.py` (`save_streams`) |
| yt-dlp options, progress/retry hooks, `retryable()` | `app/platforms/ytdlp.py`, `downloader.py` |
| Lazy aiogram pydantic models (memory) | `app/lean_models.py` |
| UI texts and translations | `app/services/captions.py`, `app/i18n/` |
| Hidden `/admin` panel (`ADMIN_IDS`): pages, actions, broadcast | `app/bot/admin/` |
| Activity tracking, bans, maintenance mode (middleware after language) | `app/bot/activity.py` |
| Statistics in Redis (`st:<bot>:*`), live download snapshots, node heartbeats | `app/services/stats.py`, `health.py`, `ops.py` |

## Conventions that matter

- **Texts**: write English in code and wrap it in `t("...")` (`app.i18n`); add the Farsi to
  `app/i18n/fa.py` with identical `{placeholders}` and HTML tags. `tests/test_i18n.py` fails on any
  untranslated text. Status constants (`captions.BUSY`, `errors.TEMPORARY`) resolve lazily to the
  current language; don't copy them into module-level variables.
- **Progress callbacks** are `progress(done, total, **detail)`; detail keys are documented at the
  top of `app/services/progress.py`. Raising `Cancelled` from them aborts yt-dlp cleanly.
- **Speed**: YouTube stalls large ranges, so requests to `googlevideo.com` stay at 10 MB or less
  (`segmented.CHUNK_CAPS`, and yt-dlp's `http_chunk_size` for its own downloader). Parallel
  connections per file: `DOWNLOAD_CONNECTIONS` (1 turns them off).
- **Memory** is the production constraint (1.5 GB slice for the whole stack: bot 512 MB, worker 1 GB). Don't add eager
  imports of heavy modules to the bot, keep downloads streaming to disk, keep deno's heap cap.
- **Never** retry or resend after Telegram may have accepted an upload (see RELIABILITY.md).
