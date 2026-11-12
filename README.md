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
