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
SoundCloud sets arrive as audio albums of up to ten tracks per batch (a final single track is sent
individually). Empty, incomplete, or larger-than-50-track sets fail explicitly instead of silently
dropping tracks. Each track retains the normal source and upload size limits.

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
| SoundCloud | yt-dlp | tracks and sets of up to 50 tracks, sent as MP3 with tags and artwork |
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

### Deploying on a shared VPS

`docker-compose.prod.yml` runs the stack as Compose project `tg_dl` inside the systemd slice
`deploy/tg_dl.slice`: 500 MB RAM in total (no swap), one CPU core, and lower disk I/O priority than
other services. Redis, Bot API and download data live on a dedicated 10 GB filesystem, so the bot
cannot fill the host disk. No ports are published and the Docker daemon configuration is unchanged.

```sh
# once, as root, in /opt/tg_dl (repository files + .env)
cp deploy/tg_dl.slice /etc/systemd/system/ && systemctl daemon-reload
truncate -s 10G /opt/tg_dl/data.img && mkfs.ext4 -q -m 0 /opt/tg_dl/data.img
mkdir -p /opt/tg_dl/data
echo '/opt/tg_dl/data.img /opt/tg_dl/data ext4 loop,noatime,nofail 0 2' >> /etc/fstab && mount /opt/tg_dl/data
mkdir -p /opt/tg_dl/data/{redis,botapi,downloads} && chown 1000:1000 /opt/tg_dl/data/downloads
# every release (image built elsewhere: docker save media-downloader-bot | ssh … docker load)
docker compose -p tg_dl -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build
```

Memory: each bot/worker process idles at ~75 MB because aiogram's ~460 pydantic models are built on
first use instead of at import (`app/lean_models.py`; eager builds cost ~175 MB per process). The
idle stack is ~300 MB, leaving room for one download (`MAX_PARALLEL_DOWNLOADS=1`) inside 500 MB;
worker pool processes are recycled after a task once they exceed 200 MB. Set
`AIOGRAM_EAGER_MODELS=1` to restore aiogram's eager model building.

Stop any other instance using the same bot token first (for example a local `uv run main.py` stack):
two Bot API servers or pollers for one token conflict.

### Without the local Bot API server
Set `BOT_API_URL=` (empty), `BOT_API_LOCAL=false`, `MAX_FILE_MB=50`. Files are then uploaded over HTTP
and anything over 50 MB is shown as ❌.

## Configuration

### Require users to join channels

Add the bot as an **administrator** of every channel, then configure `.env`. Separate several
channels with commas (up to 10); users must be members of **all** of them:

```dotenv
FORCE_JOIN_CHANNEL_ID=-1001234567890,@public_channel,-1009876543210
# Join links, matched to the IDs above by position. Leave a position empty for a public channel.
FORCE_JOIN_CHANNEL_URL=https://t.me/+first_invite,,https://t.me/+third_invite
```

A single channel works as before: `FORCE_JOIN_CHANNEL_ID=-1001234567890`. Numeric IDs and
`@channel_username` entries can be mixed. Leave `FORCE_JOIN_CHANNEL_ID` empty to disable this
feature. For a public channel, the join URL is derived from its username, so its URL position can
stay empty (trailing empty positions can be omitted). A private channel needs an explicit invite
URL unless Telegram already returns the bot's existing invite link. The bot does not create or
revoke invite links automatically. Empty entries between commas, duplicate channels (including a
username and the ID of the same channel), and more URLs than channels are rejected.

Unjoined users see a **Join …** button for each channel they have not joined yet, plus **Check
membership**, on `/start` or when trying to use a download link/button. All channels are checked
concurrently; after a check, only the channels still missing are listed. Successful verification
opens the welcome screen; users can then send their link again. Membership is checked before each
interaction, including cached resends and old buttons, so leaving any channel removes access again. Downloads already accepted while the user
was a member can finish. In groups, unrelated chatter is ignored and each membership-check button
belongs to the user who requested it.

An API failure for any channel blocks access and shows a retry message. An inaccessible channel,
missing bot admin rights, or a private channel without a join link prevents startup with a
configuration error naming that channel.
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

## Development

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

## Updating an existing installation

Pause incoming requests and let the old workers drain before replacing bot and worker together;
do not mix old and new worker versions. Then recreate the affected services:
`docker compose up -d --build redis bot worker`.
Old queued requests without a delivery journal receive an expired-request message and must be
submitted again. Legacy file-ID cache keys are ignored because they did not record bot ownership.
Source URLs and metadata keep their existing cache lifetimes.

**Disk usage.** Each download runs in its own temporary directory that is removed after delivery.
Only the `MEDIA_CACHE_ITEMS` most recent downloads (default 3) are kept, as hardlinks in
`DOWNLOAD_DIR/.media-cache`, within `MEDIA_CACHE_MAX_MB` and `MEDIA_CACHE_TTL`. A retry of the same
media/format is then served from disk. Downloads are refused while less than `MIN_FREE_DISK_MB` is
free, after old retained media has been evicted. Directories left by killed workers are swept
automatically. Details: [docs/RELIABILITY.md](docs/RELIABILITY.md#disk-management).

`MAX_FILE_MB` caps finished uploads. `MAX_SOURCE_MB` (default: 4096) separately caps input bytes for
MP3 conversion, since a large source may encode into a small MP3. Displayed MP3 sizes estimate the
192 kbps encoded output; completed files are checked again before upload. Cached Telegram files can
be resent without uploading, even when they exceed the current upload cap.

See [docs/RELIABILITY.md](docs/RELIABILITY.md) for the audit fixes, recovery limits and targeted checks.
