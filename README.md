# Media Downloader Bot

A Telegram bot that downloads videos, photos and music from links: **YouTube, Instagram, TikTok,
Pinterest, Snapchat, Likee, VK, Facebook, Threads, SoundCloud and Spotify**.

UX reference: [docs/screenshots](docs/screenshots).

1. `/start`: welcome text and an "Add to a group" button.
2. Send a link: the bot immediately acknowledges it, then shows metadata lookup status with elapsed
   time every five seconds while the site responds. That message becomes the thumbnail and an info card (channel, subscribers, duration,
   views/likes/comments, category, date, size per format) and buttons for each resolution,
   MP3, 🖼 thumbnail and 📝 description.
3. Videos with dubbed audio show a language picker (Original / DE-DE / ES-US / …).
4. The card turns into "queued", then a live `▓▓▓░░░ 40%` progress bar.
   Download status puts the current stage, percentage and received size first, followed by a compact
   title/creator/format line. Sources without a known total show received bytes instead of a fake percentage.
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

Failure handling and recovery rules are documented in [docs/RELIABILITY.md](docs/RELIABILITY.md).
Users receive specific retry/permission/size messages; unknown failures retain diagnostic logs.
Unconfirmed uploads are never retried automatically, to avoid duplicate deliveries.

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

Queue ownership and delivery progress are recorded in Redis (`app/services/requests.py`).
Queued jobs expire after `QUEUE_TTL` seconds (default: one day). Active jobs renew their owned
leases, and completed gallery batches are checkpointed so worker redelivery can resume without
resending those batches. Keep Redis persistent; Compose enables AOF with `appendfsync always`.

Telegram rate-limit responses and confirmed connection failures get bounded upload retries.
An upload whose response was lost is marked **delivery uncertain**, and is not automatically resent.
Check the chat before requesting it again. Redis and Telegram cannot provide one atomic transaction.

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
The mounted originals stay read-only. Each yt-dlp/gallery-dl operation uses and removes a private
writable cookie copy, so simultaneous requests cannot overwrite your exported cookies.

**Network access.** Platform links must use supported hostnames and HTTP(S) on ports 80/443.
Page/media connections reject private and reserved destination addresses, including after redirects.
Media clients do not use environment HTTP proxies, which could bypass these destination checks;
network-layer routing/VPNs remain usable. Only the configured PO-token provider's `GET /ping` and
`POST /get_pot` endpoints are allowed as trusted internal yt-dlp requests.

### Without the local Bot API server
Set `BOT_API_URL=` (empty), `BOT_API_LOCAL=false`, `MAX_FILE_MB=50`. Files are then uploaded over HTTP
and anything over 50 MB is shown as ❌.

## Development

### Require users to join a channel

Add the bot as an **administrator** of the channel, then configure `.env`:

```dotenv
FORCE_JOIN_CHANNEL_ID=-1001234567890
# @channel_username is also accepted instead of the numeric ID.
# Optional for public channels; use an existing invite link for a private channel:
FORCE_JOIN_CHANNEL_URL=https://t.me/+your_invite_link
```

Leave `FORCE_JOIN_CHANNEL_ID` empty to disable this feature. For a public channel, the join URL is
derived from its username; you can leave `FORCE_JOIN_CHANNEL_URL` empty. A private channel needs an
explicit invite URL unless Telegram already returns the bot's existing invite link. The bot does
not create or revoke invite links automatically.

Unjoined users see **Join channel** and **Check membership** buttons on `/start` or when trying to
use a download link/button. Successful verification opens the welcome screen; users can then send
their link again. Membership is checked before each interaction, including cached resends and old
buttons, so leaving the channel removes access again. Downloads already accepted while the user
was a member can finish. In groups, unrelated chatter is ignored and each membership-check button
belongs to the user who requested it.

An API failure blocks access and shows a retry message. An inaccessible channel, missing bot admin
rights, or a private channel without a join link prevents startup with a clear configuration error.
Only membership verification needs admin status; the gate does not require posting rights.
Restart the bot process after changing these settings (or recreate the Compose `bot` service).

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

### Updating an existing installation

Pause incoming requests and let the old workers drain before replacing bot and worker together;
do not mix old and new worker versions. Then recreate the affected services:
`docker compose up -d --build redis bot worker`.
Old queued requests without a delivery journal receive an expired-request message and must be
submitted again. Legacy file-ID cache keys are ignored because they did not record bot ownership.
Source URLs and metadata keep their existing cache lifetimes.

`MAX_FILE_MB` caps finished uploads. `MAX_SOURCE_MB` (default: 4096) separately caps input bytes for
MP3 conversion, since a large source may encode into a small MP3. Displayed MP3 sizes estimate the
192 kbps encoded output; completed files are checked again before upload. Cached Telegram files can
be resent without uploading, even when they exceed the current upload cap.

See [docs/RELIABILITY.md](docs/RELIABILITY.md) for the audit fixes, recovery limits and targeted checks.
