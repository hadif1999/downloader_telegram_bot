# Media Downloader Bot

A Telegram bot that downloads videos, photos and music from links: **YouTube, Instagram, TikTok,
Pinterest, Snapchat, Likee, VK, Facebook, Threads, SoundCloud and Spotify**.

UX reference: [docs/screenshots](docs/screenshots).

1. `/start`: welcome text and an "Add to a group" button.
2. Send a link: the bot replies with the thumbnail and an info card (channel, subscribers, duration,
   views/likes/comments, category, date, size per format) and buttons for each resolution,
   MP3, 🖼 thumbnail and 📝 description.
3. Videos with dubbed audio show a language picker (Original / DE-DE / ES-US / …).
4. The card turns into "queued", then a live `▓▓▓░░░ 40%` progress bar.
5. The video arrives with a "🎵 Download music from video" button. Each finished file's `file_id`
   is cached, so a repeat request is sent instantly.

When there is nothing to choose (one quality, a photo post, a track), the download starts right away
without the card. Photo posts and carousels arrive as Telegram albums.

## Platforms

| Platform | How | Notes |
|----------|-----|-------|
| YouTube | yt-dlp + bgutil PO-token provider | resolutions, dubbed audio tracks, MP3 |
| Instagram | yt-dlp (reels/videos), gallery-dl (photos, carousels) | most content needs `cookies/instagram.txt` |
| TikTok | yt-dlp (videos), gallery-dl (photo slideshows) | |
| Pinterest | yt-dlp (video pins), gallery-dl / OpenGraph (image pins) | `pin.it` short links work |
| Snapchat | OpenGraph `og:video` of Spotlight pages | yt-dlp's extractor is currently broken, so it's skipped |
| Likee | yt-dlp, OpenGraph fallback | untested: likee.video redirects to /notfound from our test region |
| VK | yt-dlp (videos/clips), gallery-dl (wall photos) | |
| Facebook | yt-dlp (videos/reels), gallery-dl (photos), OpenGraph | many posts need `cookies/facebook.txt` |
| Threads | page's embedded post JSON (no library supports it) | videos, photos, carousels of public posts |
| SoundCloud | yt-dlp | sent as MP3 with tags and artwork |
| Spotify | track metadata from the page, audio matched on YouTube by duration | DRM: Spotify audio itself can't be downloaded. Single tracks only |

Every platform lives in `app/platforms/`: generic yt-dlp sites are a few lines in `sites.py`
(`YtDlpPlatform` with optional `gallery` / `opengraph` fallbacks); Threads and Spotify are custom.

## Architecture

```
Telegram ⇄ telegram-bot-api (local server, 2 GB uploads)
              ⇅                           ▲ file:// (shared volume)
           bot (aiogram 3) ──enqueue──▶ redis ──▶ worker (celery, N parallel) ── yt-dlp + ffmpeg
              └──── info / file_id cache, per-user locks ────┘          └─ bgutil PO-token provider
```

| Path | Role |
|------|------|
| `app/bot/` | aiogram dispatcher and handlers (`start`, `links`, `callbacks`) |
| `app/worker/` | Celery app and the `download_media` task (progress, upload, caching) |
| `app/platforms/` | `Platform` protocol, registry, generic `ytdlp.py`, `gallery.py`, site modules |
| `app/services/` | captions/texts, keyboards, Redis cache and locks, Telegram helpers |

**Parallelism:** `MAX_PARALLEL_DOWNLOADS` sets the Celery worker concurrency. Extra jobs wait in Redis.
Each user can have one active download at a time. To scale out, run more worker containers
(`docker compose up --scale worker=3`): total parallelism is then workers × concurrency.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
   For group support, turn privacy mode off (`/setprivacy` → Disable) so the bot can see plain links.
2. Get `api_id` / `api_hash` at <https://my.telegram.org> → API development tools.
3. `cp .env.example .env` and fill in `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
4. **Once only**, log the bot out of the cloud Bot API so the local server can serve it:
   `curl https://api.telegram.org/bot<BOT_TOKEN>/logOut`
5. `docker compose up -d --build`, then `docker compose logs -f bot worker`.

**Cookies.** Many sites (Instagram, Facebook, sometimes YouTube with "sign in to confirm you're not a
bot") only serve logged-in users. Export cookies from a browser in Netscape format (e.g. the
"Get cookies.txt LOCALLY" extension) and save them as `cookies/<site>.txt`, e.g. `cookies/instagram.txt`,
`cookies/facebook.txt`, `cookies/youtube.txt`. They are used automatically by both yt-dlp and gallery-dl.
Use throwaway accounts.

### Without the local Bot API server
Set `BOT_API_URL=` (empty), `BOT_API_LOCAL=false`, `MAX_FILE_MB=50`. Files are then uploaded over HTTP
and anything over 50 MB is shown as ❌.

## Development

### Enable or disable platforms

All platforms are disabled by default when their flag is omitted. Set a flag to `true` in `.env`
to enable that platform:

```dotenv
YOUTUBE_ENABLED=false
INSTAGRAM_ENABLED=false
TIKTOK_ENABLED=false
PINTEREST_ENABLED=false
SNAPCHAT_ENABLED=false
LIKEE_ENABLED=false
VK_ENABLED=false
FACEBOOK_ENABLED=false
THREADS_ENABLED=false
SOUNDCLOUD_ENABLED=false
SPOTIFY_ENABLED=false
```

Disabled platforms are hidden from `/start` and `/help`, their links are ignored, and old download
buttons stop working. Apply changes to both bot and worker with
`docker compose up -d --force-recreate bot worker` (or restart both processes when running locally).
Disabling YouTube links does not disable Spotify's internal YouTube audio matching.

### Local development

Install Docker with Compose, ffmpeg, and Deno on your host. Set `BOT_TOKEN` in `.env` and enable
at least one platform, for example `YOUTUBE_ENABLED=true`. Then run:

```bash
uv run main.py
```

This starts Redis and the PO-token provider in Docker and runs both
the bot and Celery worker locally with interleaved logs. Without `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH`, it uses Telegram's cloud Bot API with a maximum 50 MB upload limit.
To use the local Bot API for larger uploads, set both credentials and perform the one-time
cloud Bot API logout described in Setup; the launcher then starts that service too.
Default Docker service addresses become
localhost addresses automatically; default downloads/cookies paths become project directories.
Host files upload over HTTP to the local Bot API, so no host download-volume mapping is needed.
Explicit custom service URLs and directory paths are preserved. The launcher leaves `.env` unchanged.
Ports 6379 and 4416 (plus 8081 in local Bot API mode) must be available; they bind only to localhost.
Deno is also detected at `~/.deno/bin/deno` if its install directory is missing from `PATH`.

Do not run the Compose `bot` / `worker` containers at the same time as the local processes:
`docker compose stop bot worker`. Ctrl+C stops the local bot and worker; active downloads have
20 seconds to stop before being killed. The infrastructure containers remain running for reuse.
Stop those with `docker compose -f docker-compose.yml -f docker-compose.local.yml stop redis telegram-bot-api bgutil-provider`.

For separate processes or development checks:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
# run locally against a local redis / bot-api:
uv run python -m app.bot.main
uv run celery -A app.worker.celery_app worker --concurrency=2 -l INFO
```

Keep the extractors up to date: `uv lock --upgrade-package yt-dlp --upgrade-package gallery-dl`.
Sites change often.
