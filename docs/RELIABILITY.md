# Download and delivery reliability

## Request lifecycle

Every download and cached resend has a UUID request ID, scoped to the bot ID in Redis. Creating a
request atomically reserves that user's download slot and records `queued` state. Queue publication
runs outside the bot event loop, with the same UUID as the Celery task ID and an explicit expiry.
If publication fails before a worker claims the request, the journal is cancelled and the card is
restored. If the worker already claimed or completed it, the producer preserves that outcome.

A worker claims an execution lease with a unique owner token. Queued user locks outlive the queue
deadline; active leases are renewed every 30 seconds with a 90-second lifetime. Renew, release,
and delivery transitions verify ownership atomically with Lua. A stale worker cannot remove another
worker's lease or another request's user lock. A redelivery that finds a live owner waits and retries
the claim; it does not acknowledge away recovery of a crashed original.

Before an upload/batch the worker persists `sending`. After Telegram accepts it, the worker records
file IDs and its item offset. A replay skips checkpointed gallery items and completed requests.
Definite rate-limit responses and connection-establishment failures retry up to three attempts while
keeping the already-downloaded files. Other network failures or lost send responses are uncertain:
the request stops automatic sends and tells the user to check the chat. The next request is an
explicit user decision, not a blind background repeat.

An accepted upload remains successful if saving its file-ID cache entry fails. If a batch was accepted
but its checkpoint cannot be persisted, an unfinished gallery is reported uncertain. A fully accepted
result is not falsely reported as failed. Unexpected download/delivery errors propagate to Celery
after updating the user-facing status, rather than being counted as successful worker tasks.

## Persistence and deployment

Redis is required state, not merely a disposable cache. Compose uses AOF and `appendfsync always`
so a successful intent write survives a normal Redis process restart. Keep the Redis volume and use
the equivalent durability configuration with a custom Redis server. Restoring an older Redis backup
while jobs run can invalidate delivery history; coordinate restores with paused bot/workers.

There is no atomic transaction spanning Redis and Telegram. The conservative `sending`/uncertain
state can stop a request that crashed immediately before Telegram actually received it. It prevents
an automatic duplicate at the cost of requiring the user to check the chat and explicitly retry.
Do not claim exactly-once delivery across network partitions or restored/lost Redis data.

For the first upgrade, pause producers and drain old workers before updating both together. Legacy
tasks without request journals are expired with a resend prompt. Existing unscoped Telegram file-ID
keys are ignored; cached IDs are now bot-specific, and invalid IDs are evicted on demand. Metadata
and source URL cache entries are unchanged. Do not clear deployment Redis during ordinary upgrades.

Limits: `QUEUE_TTL` defaults to 86400 seconds (60 seconds–7 days); `TASK_TIME_LIMIT` defaults to
3600 seconds (120 seconds–24 hours). `MAX_FILE_MB` caps final files; `MAX_SOURCE_MB` defaults to
4096 MB and bounds inputs that will be converted to MP3. Normal cleanup removes per-attempt UUID
directories and temporary cookie/thumbnail copies. A hard OS/container kill can leave an orphaned
attempt directory; workers remove those once they are older than `TASK_TIME_LIMIT` + 10 minutes
(at worker startup and after every download).

## Disk management

Finished files are hardlinked into `DOWNLOAD_DIR/.media-cache` before the attempt directory is
removed, so retention costs no extra space during upload. At most `MEDIA_CACHE_ITEMS` (default 3)
entries are kept, least recently used first out, bounded by `MEDIA_CACHE_MAX_MB` in total and
`MEDIA_CACHE_TTL` since last use. A worker serves a matching platform/media/format/language from
this store instead of downloading it again, e.g. when the user retries after an uncertain upload.
Telegram `file_id` resends remain the first choice and need no disk at all.

Workers coordinate through `flock`: entries being uploaded hold a shared pin and are never evicted;
eviction is skipped for them and retried after the next download. Before a download the worker
requires `MIN_FREE_DISK_MB` plus the estimated size (twice the output for MP3 conversion) to be free.
If not, it evicts all unpinned retained media and otherwise refuses with a low-storage message.
Failures to store or read retained media fall back to a normal download. The store must be on the
same filesystem as the attempt directories (it is, under `DOWNLOAD_DIR`); otherwise nothing is
retained.

## Network and media contracts

Platform matchers parse the hostname before applying path patterns. Python page/media transports
allow HTTP(S) on standard ports and reject all private/reserved/multicast DNS answers. The actual
socket connects to the already-validated address, preserving the original TLS hostname, so a second
DNS resolution cannot bypass the check. Redirects use the same guarded transport. yt-dlp and the
isolated gallery-dl extraction process use these connection boundaries. Environment media proxies
are disabled, since proxy-side DNS could bypass the check. Trusted Telegram/Redis configuration is
separate; yt-dlp's configured PO-token sidecar is allowed only at its two control endpoints.

yt-dlp's urllib adapter and the requests/urllib3 gallery adapter are covered by transport contract
tests. Rerun those tests when upgrading the extractors or HTTP dependencies. Docker installs the
committed lockfile with `uv sync --frozen`; do not bypass the lockfile for deployment.

Cookie sources stay read-only. Every extractor instance/subprocess gets a private writable copy.
Format choices are validated against refreshed metadata; no fallback can exceed the requested
height or silently drop a requested audio language. For a progressive video, explicit language
selection merges the selected audio and removes the embedded source audio. If the requested media
cannot be selected, the bot fails clearly instead of substituting another quality/track.

Single gallery items use individual photo/video methods; albums use groups of 2–10. If complete
carousel extraction fails, the bot reports the failure instead of silently reducing it to item one.
Animated images converted into still photos use one frame. Thumbnail documents are uploaded from
bounded local files, not unsupported remote JPEG document URLs.

Caption construction retains complete HTML, bounds visible UTF-16 lengths, and reserves room for
status/action text. Descriptions are split as plain text and sent with HTML parsing disabled.
Callbacks acknowledge immediately and share the same four-slot extraction limiter as new links.
Synchronous Redis and Celery publication run in threads with bounded service timeouts.

Download status messages put the current stage first, followed by a monospaced progress bar,
percentage, and received/total amount when available. The title, creator, chosen format/language,
and duration form a compact footer; the full metadata stays on the format-selection/result cards.
Before the first bytes arrive the status says "Preparing download". An unknown total shows received
bytes or file counts without a fabricated percentage or a lone question mark. Photo-caption and
plain-text status messages use the same layout through queueing, download and upload.

The download thread only records progress in `app/services/progress.Monitor` (under a lock); the
worker's event loop renders and edits the card on a timer, so a slow or rate-limited Telegram edit
never stalls the download, and edits are serialized so a late tick can't overwrite a newer stage.
Cancel: a queued request is cancelled atomically in Redis (`request_cancel`) and the bot redraws the
card. A running one gets a cancel flag that the worker polls each tick; the next progress report
raises `Cancelled` (a yt-dlp `DownloadCancelled`, which yt-dlp lets propagate from its hooks), the
journal moves to `cancelled`, files are removed and nothing is sent. The flag is checked once more
right before upload; once the journal reaches `sending`, cancellation is refused as too late.
Only the user stored on the request may cancel it.

## Regression coverage for the 20 audit findings

| Finding | Fix | Targeted evidence |
|---|---|---|
| 1. Unrelated/internal URL destinations | Hostname allowlists, pinned public connections, redirect checks | `test_network.py` |
| 2. Read-only cookie write errors | Per-operation writable cookie copies and cleanup | `test_network.py` |
| 3. Singleton albums/carousel tails | Individual sends for singleton batches | `test_delivery.py` |
| 4. Expired/unowned user locks | Atomic request creation and owned renewable leases | `test_requests.py` (real isolated Redis Lua) |
| 5. Ignored resolution/stale choice | Validate format key; bounded selectors | `test_jobs.py`, `test_media_downloads.py` |
| 6. Ignored dubbed language | Strict language selection and embedded-audio removal | `test_media_downloads.py` (real generated audio/video) |
| 7. Broken/oversized captions | Bound visible fields and compose complete markup | `test_captions_safety.py` |
| 8. Split escaped entities | Plain-text Unicode-safe chunks, no HTML parsing | `test_captions_safety.py` |
| 9. Wrong MP3 size gate | Output bitrate estimates, distinct source/output caps | `test_jobs.py`, `test_media_downloads.py` |
| 10. Hidden usable audio | Keep the card when the only video is oversized | `test_captions_safety.py` |
| 11. False queued cards | Cancel unclaimed publish failures; restore controls | `test_jobs.py` |
| 12. Poisoned cached IDs | Bot namespace; evict invalid IDs and queue fallback | `test_requests.py`, `test_jobs.py` |
| 13. Truncated carousel | Explicit failure if all entries cannot be preserved | `test_media_downloads.py` |
| 14. GIF frame conversion | Convert one frame to a JPEG | `test_media_downloads.py` (real ffmpeg) |
| 15. Duplicate worker redelivery | Request/batch journal; conservative uncertain state | `test_requests.py`, `test_worker_delivery.py` |
| 16. False failure after upload | Separate cache persistence from delivered outcome | `test_worker_delivery.py` |
| 17. Callback spinner/limit bypass | Early acknowledgement and shared extraction slots | `test_captions_safety.py` |
| 18. Blocked bot event loop | Redis and publication offloaded to threads | `test_jobs.py` |
| 19. Unsupported thumbnail URL document | Bounded local upload and cleanup | `test_captions_safety.py` |
| 20. No safe upload recovery | Bounded definite-failure retries; uncertain outcomes stop | `test_delivery.py`, `test_worker_delivery.py` |

## Error handling and recovery

Bot updates have a final error observer in addition to contextual link/callback handling. Expected
failures receive specific advice for unavailable source media, upload limits, permissions, rate
limits, temporary infrastructure failures, processing failures, and timeouts. Only explicit
`UserError` messages are displayed; they are escaped and bounded. Unknown exception details stay
in logs. Disabled/obsolete platforms are distinguished from unexpected `KeyError` bugs.

Callback acknowledgements and error feedback use five-second deadlines. Expired acknowledgements
do not abort a valid action. A deleted status can receive one fallback message, while an ambiguous
feedback timeout is not retried. Status edits are best effort: a timeout or malformed status response
cannot fail the download. Progress-task and session-cleanup failures cannot replace the primary
outcome. Normal bot API calls have a 30-second session timeout; worker upload calls retain the
existing longer deadline capped by the task limit.

The worker also reports request-store failures that happen before lease acquisition, closes its bot
session, and still raises the original error for Celery monitoring. A definite queue-publication
failure updates either the original format card or a newly created progress message. Redis failures
are not treated as successful queue operations or cache misses. Unreadable metadata entries are
evicted individually for re-extraction; request journals and stored source URLs remain intact.

Upload responses must contain the expected number of media messages and valid file IDs before a
batch is checkpointed. A malformed/partial response or cancellation during upload leaves an unknown
delivery outcome and prevents automatic resend. HTTP 413 is a definite size rejection, even though
aiogram represents it as a network-error subclass. Failure to record a definite rejection preserves
the original exception. Once delivery is confirmed, cache, final-checkpoint, and cleanup failures do
not turn it into a reported download failure.

Bot and Celery log formatters mask bot tokens, URL credentials, query strings, and fragments in
messages and tracebacks. Logs retain request/update identifiers and stack traces for diagnosis.
This protects those particular values; it is not a general-purpose scrubber for arbitrary secrets
inside third-party output. Avoid logging raw configuration or cookie contents.

Targeted verification for this change: **193 tests passed** across error handling, link feedback,
delivery, worker delivery, queueing, caption safety, startup, and membership. These include real
isolated Redis journal checks and mocked Telegram/Dispatcher failures. Error feedback cannot be
guaranteed while Telegram is unreachable or the bot is blocked. Hard process kills, external-service
compatibility, live rollout, and full-suite/release validation were not exercised.

## Running checks

The test setup disables deployment dotenv loading and external sockets/DNS, including thumbnail
probes during fixture collection. Request-store tests use the already available `redis:7-alpine`
image with `--pull=never`, no network, no published ports, and a private Unix socket. They never
connect to deployment Redis or Telegram. If Docker/the image is unavailable those tests are skipped;
treat the Redis integration paths as unverified until they run. Media tests use tiny generated local
fixtures and require installed ffmpeg/ffprobe. No browsers or live platform/account checks are needed.

Select the affected files, for example:

```sh
rtk proxy timeout 180 uv run pytest -q tests/test_requests.py tests/test_delivery.py tests/test_worker_delivery.py
rtk proxy timeout 180 uv run pytest -q tests/test_network.py tests/test_media_downloads.py
rtk proxy timeout 180 uv run pytest -q tests/test_jobs.py tests/test_captions_safety.py tests/test_link_feedback.py
rtk proxy timeout 60 uv run ruff check app tests
rtk proxy timeout 60 uv run ruff format --check app tests
```

Live Telegram/platform compatibility, browser/E2E checks, Docker rollout, hard-kill/restore scenarios,
and full repository/release validation require separate explicit validation. Targeted mocked
Telegram responses and local Redis/media checks do not prove those external behaviors.

### Verification recorded for this change

On 2026-11-14, all **187 targeted cases** in the twelve affected test files passed across scoped runs,
including real isolated Redis Lua/concurrency checks and generated ffmpeg/yt-dlp media checks.
No test was skipped. Ruff lint, formatting, diff whitespace checks, frozen offline environment sync,
and offline lockfile consistency passed. The local test interpreter was Python 3.14; the source
continues to target Python 3.12, but the Python 3.12 Docker deployment was not run as a release check.
