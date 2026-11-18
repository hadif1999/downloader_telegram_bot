# Deployment runbook (shared VPS)

The bot runs as Docker Compose project **`tg_dl`** in `/opt/tg_dl` on a VPS that also hosts other
stacks. Everything below is designed so the bot can never starve or disturb those services.
Host-specific values (address, SSH interface) live in the gitignored `CLAUDE.local.md`.

## Layout on the host

| Path | What |
|---|---|
| `/opt/tg_dl/` | repository files (from `git archive`), `.env` (mode 600, never committed), `RELEASE` |
| `/opt/tg_dl/data.img` | 10 GB sparse ext4 image, loop-mounted at `/opt/tg_dl/data` via `/etc/fstab` |
| `/opt/tg_dl/data/{redis,botapi,downloads}` | bind-backed named volumes (owners: botapi `101:101`, downloads `1000:1000`) |
| `/etc/systemd/system/tg_dl.slice` | copy of `deploy/tg_dl.slice`: 1 CPU, MemoryHigh 1440M, MemoryMax 1536M, no swap, IOWeight 50 |
| `/opt/tg_dl/build.log` | log of the last image build |

Containers (`docker compose -p tg_dl ps`): `redis`, `telegram-bot-api` (local Bot API, 2 GB
uploads), `bgutil-provider` (YouTube PO tokens), `bot` (aiogram polling), `worker` (Celery,
`MAX_PARALLEL_DOWNLOADS=2`). All run with `cgroup_parent: tg_dl.slice`; no ports are published.

## Memory budget (1.5 GB slice)

| Part | Typical | Limit |
|---|---|---|
| redis + bot-api + bgutil | ~150 MB | 96m / 192m / 192m |
| bot idle + `EXTRACT_SLOTS=4` lookups | ~90 MB + ~40-60 MB per lookup (no JS solver) | 512m |
| worker idle (2 pool processes) | ~160 MB | 1g |
| `MAX_PARALLEL_DOWNLOADS=2` downloads | worker peak ~540 MB incl. page cache | in worker |

Measured: two parallel 1080p/720p downloads peak at ~960 MB for the slice including reclaimable
page cache (~560 MB anon) with no OOM kills. Per-service limits only stop one container from
starving its siblings; the slice is the real cap. YouTube's JS challenge solver is off
(`YOUTUBE_JS_RUNTIMES` empty): deno needs ~320 MB per solve, which this budget can't afford.

## First install (once, as root)

```sh
cd /opt/tg_dl                                   # repository files + .env
cp deploy/tg_dl.slice /etc/systemd/system/ && systemctl daemon-reload
truncate -s 10G data.img && mkfs.ext4 -q -m 0 data.img && mkdir -p data
cp /etc/fstab /root/fstab.bak-before-tg_dl
echo '/opt/tg_dl/data.img /opt/tg_dl/data ext4 loop,noatime,nofail 0 2' >> /etc/fstab && mount data
mkdir -p data/{redis,botapi,downloads} && chown 101:101 data/botapi && chown 1000:1000 data/downloads
# direct I/O for the loop device (otherwise every byte is cached twice and merges crawl)
cp deploy/tg_dl-loop-dio.service /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now tg_dl-loop-dio.service && losetup -l -O NAME,BACK-FILE,DIO
```

### Inline mode (once)

In @BotFather: `/setinline` (placeholder text) and `/setinlinefeedback` -> Enabled. Create a
private channel, add the bot as an admin with "Post messages", and put its ID in `.env` as
`INLINE_STORAGE_CHAT_ID`. The bot refuses to start if it can't post there, so check
`docker compose -p tg_dl logs bot` after the first start with it. Replacing a text card with
media (`editMessageMedia` on an inline text message) needs a recent Bot API server: pull
`aiogram/telegram-bot-api:latest` before that release
(`docker compose -p tg_dl -f docker-compose.yml -f docker-compose.prod.yml pull telegram-bot-api`).

## Release

Uploads to the VPS are slow (~150 KB/s), so the image is **built on the VPS** from the source,
with a builder capped at 500 MB / 1 CPU so the build can't hurt the other stacks.

```sh
# 1. from the dev machine: ship source and config (secrets never go through git)
git archive --format=tar HEAD | gzip | ssh root@HOST 'cd /opt/tg_dl && gunzip | tar -x'
ssh root@HOST 'umask 077; cat > /opt/tg_dl/.env.new && mv /opt/tg_dl/.env.new /opt/tg_dl/.env' < .env

# 2. on the VPS: build (detached; watch build.log)
cd /opt/tg_dl
docker buildx inspect tgdl-builder >/dev/null 2>&1 || docker buildx create --name tgdl-builder \
  --driver docker-container --driver-opt memory=500m --driver-opt memory-swap=500m \
  --driver-opt cpu-quota=100000 --driver-opt cpu-period=100000
REV=<git short sha>
nohup nice -n 10 docker buildx build --builder tgdl-builder --load -t media-downloader-bot:$REV . > build.log 2>&1 &
# wait: `pgrep -x docker-buildx` (never `pgrep -f "buildx build"`: it matches the ssh shell itself)

# 3. switch
docker tag media-downloader-bot:$REV media-downloader-bot:latest
cp deploy/tg_dl.slice /etc/systemd/system/ && systemctl daemon-reload   # if the slice changed
docker compose -p tg_dl -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build
echo $REV > RELEASE

# 4. clean up: the builder holds ~1 GB of cache and an idle container
docker buildx rm tgdl-builder
docker image ls media-downloader-bot      # keep the previous tag for rollback, prune older ones
```

After changing the slice file, `systemctl daemon-reload` is not always enough: an earlier
`systemctl set-property --runtime` leaves drop-ins in `/run/systemd/system.control/tg_dl.slice.d/`
that win over the unit file until reboot. Apply live limits with
`systemctl set-property --runtime tg_dl.slice MemoryHigh=1440M MemoryMax=1536M` and confirm with
`cat /sys/fs/cgroup/tg_dl.slice/memory.max`.

## Verify

```sh
docker compose -p tg_dl ps                                   # all Up, bot/worker not restarting
docker compose -p tg_dl logs --since 5m bot worker | tail -50
#   bot: "Start polling" / "Run polling for bot @..."; worker: "celery@... ready."
systemctl status tg_dl.slice | head -8                       # Memory: current vs max
docker stats --no-stream $(docker compose -p tg_dl ps -q)
docker ps --format '{{.Names}} {{.Status}}' | grep -v tg_dl  # the other stacks stay healthy
```

Functional check from Telegram: `/admin` from an `ADMIN_IDS` account (dashboard opens; 🖥 System
lists the bot and the worker), `/start` (Farsi welcome with the `/language` hint), `/language`
(🇮🇷/🇬🇧 buttons), a short video link (format card, live progress card, file delivered), and
`✖️ لغو دانلود` on a large format, and inline mode: `@bot <link>` in a chat the bot isn't in (results
with thumbnails; picking one shows progress, then the file; the same format again is instant).

A download can also be exercised without Telegram, inside the worker container:

```sh
docker compose -p tg_dl exec -T worker python - <<'EOF'
from pathlib import Path; import tempfile
from app.platforms import registry
p, mid, url = registry.detect("https://www.youtube.com/watch?v=jNQXAC9IVRw")
info = p.extract(url); print(info.title, [f.key for f in info.formats])
with tempfile.TemporaryDirectory(dir="/downloads") as d:
    r = p.download(info, info.formats[0].key, None, Path(d), lambda *a, **k: None)
    print(r.path, Path(r.path).stat().st_size)
EOF
```

## Rollback

```sh
docker tag media-downloader-bot:<previous sha> media-downloader-bot:latest
docker compose -p tg_dl -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build
```
Redis state (queue, file_id cache, language choices) is compatible across these releases.

## Rules for this host

- Touch only Compose project `tg_dl`, `/opt/tg_dl`, `tg_dl.slice` and the `tgdl-builder` builder.
  Never `docker system prune`, restart the daemon, or change other projects' containers/networks.
- Only one instance per bot token: stop any local stack (`docker compose down`) before testing on
  the VPS, or the two Bot API servers/pollers conflict.
- `.env` stays mode 600; never print token values (check with `sha256sum .env | cut -c1-12`).
- Use `ssh -T` with a `timeout` for scripted commands; sessions over the VPN can hang otherwise.
