"""Small HTTP / parsing helpers shared by platforms."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import html
import http.client
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import get_settings
from app.platforms.network import open_url

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# link-preview crawlers get server-rendered OpenGraph tags from JS-heavy sites (Spotify, Snapchat)
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
}

TRACKING_PARAMS = re.compile(r"^(utm_\w+|igsh|igshid|si|fbclid|feature|share_id|_r|_t|ref|context)$", re.I)


def cookie_file(platform: str) -> Path | None:
    p = get_settings().cookies_dir / f"{platform}.txt"
    return p if p.is_file() else None


def clean_url(url: str) -> str:
    """Drop tracking query params and fragments so the same media maps to the same cache key."""
    u = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not TRACKING_PARAMS.match(k)]
    host = u.netloc.lower().removeprefix("m.").removeprefix("www.")
    return urllib.parse.urlunsplit(
        (u.scheme or "https", host, u.path.rstrip("/"), urllib.parse.urlencode(query), "")
    )


def url_key(url: str) -> str:
    """Short stable id for urls without a natural id (fits Telegram's 64-byte callback data)."""
    return hashlib.sha1(clean_url(url).encode()).hexdigest()[:12]


def fetch(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[str, str]:
    """GET a page -> (final url, text)."""
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    with open_url(req, timeout=timeout) as r:
        page = r.read(8 * 1024 * 1024 + 1)
        if len(page) > 8 * 1024 * 1024:
            raise ValueError("Platform page is too large.")
        return r.geturl(), page.decode("utf-8", "replace")


def og_tags(page: str) -> dict[str, str]:
    """OpenGraph / twitter / music meta tags (first value wins)."""
    tags: dict[str, str] = {}
    for m in re.finditer(r"<meta\s+([^>]+?)/?>", page, re.I):
        attrs = dict(re.findall(r'(\w[\w:-]*)\s*=\s*"([^"]*)"', m.group(1)))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            tags.setdefault(key.lower(), html.unescape(attrs["content"]))
    return tags


CHUNK = 1 << 20  # read/write unit: memory held per download is one chunk, never the file
# Written data is flushed to disk and dropped from the page cache this often. Kept well below the
# part size: unflushed (dirty) pages cannot be reclaimed, and with 200 MB between flushes they alone
# can exceed a small container memory cap (measured: 163 MB dirty for 200 MB parts, 26 MB for 32 MB).
FLUSH_BYTES = 16 << 20
CONTENT_RANGE = re.compile(r"bytes (?:(\d+)-(\d+)|\*)/(\d+|\*)", re.I)
RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}
_sleep = time.sleep  # patched in tests


def retry_delay(attempt: int) -> int:
    """Back-off before retry number `attempt` (1-based): 2, 4, 8, then 10 s."""
    return min(2**attempt, 10)


def network_error(error: BaseException) -> bool:
    """Transient transfer failures worth retrying: timeouts, dropped connections, 5xx/429."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRY_HTTP
    if yt := sys.modules.get("yt_dlp.networking.exceptions"):  # only loaded where yt-dlp is in use
        if isinstance(error, yt.HTTPError):
            return error.status in RETRY_HTTP
        if isinstance(error, yt.TransportError):
            return not isinstance(error, yt.CertificateVerifyError)
    if isinstance(error, urllib.error.URLError):
        return not isinstance(error.reason, str) or "timed out" in error.reason
    return isinstance(error, (TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError))


def describe(error: BaseException) -> str:
    """Short, user-facing reason for a retry."""
    text = str(error).lower()
    code = getattr(error, "code", None) or getattr(error, "status", None)
    if isinstance(error, TimeoutError) or "timed out" in text or "timeout" in text:
        return "the server stopped sending data"
    if isinstance(code, int) and code >= 400:
        return f"the server replied {code}"
    if (
        "short part" in text
        or "incomplete" in text
        or "bytes read" in text
        or isinstance(error, http.client.IncompleteRead)
    ):
        return "the connection dropped mid-transfer"
    if isinstance(error, (ConnectionError, ssl.SSLError, urllib.error.URLError)) or "connection" in text:
        return "the connection was lost"
    return "a network error"


class ShortPart(ConnectionError):
    pass


def download_file(
    url: str,
    dest: Path,
    referer: str | None = None,
    timeout: float | None = None,
    progress=None,
    max_bytes: int | None = None,
) -> Path:
    """HTTP download streamed straight into `dest`, over adaptive parallel connections.

    Servers with byte ranges and a known size go through `segmented.SegmentedDownload`: up to
    DOWNLOAD_CONNECTIONS connections, each writing its own range at its offset in the final file,
    with request sizes that adapt to the speed and resume from `<dest>.segments.json`. Others are
    streamed over one connection (`_download_stream`), resuming from the size of `dest`.
    Written data is pushed to disk in the background and dropped from the page cache, which keeps
    the memory charged to the container flat even for multi-gigabyte files.

    Failures are retried DOWNLOAD_RETRIES times per request, resuming from the last byte written,
    so nothing already downloaded is fetched again. `timeout` (default DOWNLOAD_TIMEOUT) is a
    stall timeout: an attempt fails when no data arrives for that long, however long it takes.

    progress(done, total, connections=) is called from this thread only (at most every 0.5 s on
    the parallel path); `total` is None while unknown. Before each retry it is called with
    retry=, retries=, reason=, wait=, kept= instead."""
    from app.platforms.segmented import NotSegmentable, SegmentedDownload

    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    s = get_settings()
    timeout = s.download_timeout if timeout is None else timeout
    limit = s.max_file_bytes if max_bytes is None else max_bytes
    report = progress or (lambda *a, **k: None)

    def on_retry(error, attempt, retries, wait, kept):
        report(kept, None, retry=attempt, retries=retries, reason=describe(error), wait=wait, kept=kept)

    def opener(url, headers, timeout):
        return open_url(urllib.request.Request(url, headers=headers), timeout=timeout)

    try:
        SegmentedDownload(
            url,
            dest,
            opener,
            headers=headers,
            timeout=timeout,
            limit=limit,
            retries=s.download_retries,
            max_conns=s.download_connections,
            min_split=s.download_min_split_bytes,
            chunk_cap=s.download_chunk_max_bytes,
            progress=report,
            on_retry=on_retry,
        ).run()
    except NotSegmentable:
        _download_stream(
            url, dest, headers, timeout, limit, s.download_retries, s.download_chunk_max_bytes, report
        )
    return dest


def _download_stream(url, dest: Path, headers, timeout, limit, retries, part, report) -> None:
    """One connection: servers without byte ranges, or without a known size."""
    start = dest.stat().st_size if dest.exists() else 0
    writeback = Writeback()
    with open(dest, "r+b" if start else "wb") as f:
        f.seek(start)
        done, total, ranged = start, None, True
        while True:
            end = done + part - 1 if total is None else min(done + part, total) - 1
            for attempt in range(retries + 1):
                try:
                    done, total, ranged = _fetch(
                        url, headers, timeout, f, done, end, total, limit, report, writeback
                    )
                    break
                except Exception as error:
                    if attempt == retries or not network_error(error):
                        raise
                    f.flush()
                    done = f.tell() if ranged else 0  # bytes on disk are kept for the next attempt
                    wait = retry_delay(attempt + 1)
                    report(
                        done,
                        total,
                        retry=attempt + 1,
                        retries=retries,
                        reason=describe(error),
                        wait=wait,
                        kept=done,
                    )
                    _sleep(wait)
            f.flush()
            writeback.step(f.fileno(), done)
            if not ranged or total is None or done >= total:
                break
        f.truncate(done)  # a resumed file may hold stale bytes past the end
        release_page_cache(f)  # once per file: everything on disk, nothing left in the cache
        if total is not None and done != total:
            raise ConnectionError(f"Incomplete download: {done} of {total} bytes")


def _fetch(
    url, headers, timeout, f, start, end, total, limit, progress, writeback=None
) -> tuple[int, int | None, bool]:
    """Fetch bytes start..end into `f`. Returns (bytes on disk, total size or None, ranged)."""
    req = urllib.request.Request(url, headers={**headers, "Range": f"bytes={start}-{end}"})
    try:
        response = open_url(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code != 416:
            raise
        # Nothing at or after `start`: either a resumed file that is already complete, or an
        # empty resource that rejects ranges. Ask for the size, or the body, without a range.
        whole = CONTENT_RANGE.fullmatch((error.headers or {}).get("Content-Range") or "")
        if start and whole and whole.group(3) != "*" and int(whole.group(3)) == start:
            return start, start, True
        response = open_url(urllib.request.Request(url, headers=headers), timeout=timeout)
    with response as r:
        match = CONTENT_RANGE.fullmatch(r.headers.get("Content-Range") or "")
        if getattr(r, "status", 200) == 206 and match and match.group(1) and int(match.group(1)) == start:
            size = None if match.group(3) == "*" else int(match.group(3))
            if total is not None and size not in (None, total):
                raise ConnectionError("The file changed on the server mid-download")
            if size is not None and size > limit:
                raise TooLargeError(url)
            expected = int(match.group(2)) + 1
            f.seek(start)
            done = _copy(r, f, start, size, limit, progress, url, writeback)
            if done != expected:
                raise ShortPart(f"Short part: {done - start} of {expected - start} bytes")
            return done, size, size is not None
        if start and total is not None:
            raise ConnectionError("Server stopped honouring byte ranges mid-download")
        # No Range support (or a resume the server ignored): stream the whole body from byte 0.
        size = int(r.headers.get("Content-Length") or 0) or None
        if size and size > limit:
            raise TooLargeError(url)
        f.seek(0)
        f.truncate()
        done = _copy(r, f, 0, size, limit, progress, url, writeback)
        if size is not None and done != size:
            raise ShortPart(f"Short download: {done} of {size} bytes")
        return done, size if size is not None else done, False


def _copy(r, f, done: int, total, limit: int, progress, url: str, writeback: Writeback | None = None) -> int:
    since_flush = 0
    while chunk := r.read(CHUNK):
        f.write(chunk)
        done += len(chunk)
        if done > limit:
            raise TooLargeError(url)
        if progress:
            progress(done, total)
        since_flush += len(chunk)
        if since_flush >= FLUSH_BYTES:
            f.flush()
            if writeback is not None:
                writeback.step(f.fileno(), done)
            else:
                release_page_cache(f)
            since_flush = 0
    return done


# sync_file_range(2) flags
_WAIT_BEFORE, _WRITE, _WAIT_AFTER = 1, 2, 4
try:
    _sync_file_range = ctypes.CDLL(None, use_errno=True).sync_file_range
    _sync_file_range.argtypes = [ctypes.c_int, ctypes.c_int64, ctypes.c_int64, ctypes.c_uint]
except (OSError, AttributeError):  # not Linux
    _sync_file_range = None


class Writeback:
    """Keep a growing file's page cache bounded without stalling the writer.

    Each step starts *background* writeback of the bytes written since the last step and drops
    the previous window, which the kernel has written meanwhile, from the page cache. At most
    two windows (~2 x FLUSH_BYTES) are cached at any time, and the writer never waits for a
    synchronous flush (fdatasync every 16 MB used to stall downloads and merges on slow disks).
    `start` is where the written region begins: each parallel connection keeps its own windows.
    """

    def __init__(self, start: int = 0) -> None:
        self.dropped = start  # bytes [start, dropped) are written and evicted
        self.started = start  # bytes [dropped, started) are being written in the background

    def step(self, fd: int, upto: int) -> None:
        if upto <= self.started:
            return
        if _sync_file_range is None or not hasattr(os, "posix_fadvise"):
            self.started = upto
            return
        if self.started > self.dropped:
            span = self.started - self.dropped
            _sync_file_range(fd, self.dropped, span, _WAIT_BEFORE | _WRITE | _WAIT_AFTER)
            with contextlib.suppress(OSError):
                os.posix_fadvise(fd, self.dropped, span, os.POSIX_FADV_DONTNEED)
            self.dropped = self.started
        _sync_file_range(fd, self.started, upto - self.started, _WRITE)
        self.started = upto

    def step_path(self, path, upto: int) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            self.step(fd, upto)
        finally:
            os.close(fd)


def release_page_cache(target) -> None:
    """Write a file's dirty pages to disk and drop its cached pages (Linux; no-op elsewhere).

    Page cache for files written in a container is charged to that container's memory limit.
    Dropping it after each part keeps multi-gigabyte downloads from pressing against the cap."""
    if not hasattr(os, "posix_fadvise"):
        return
    try:
        if isinstance(target, (str, Path)):
            fd = os.open(target, os.O_RDONLY)
            try:
                os.fdatasync(fd)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
        else:
            target.flush()
            os.fdatasync(target.fileno())
            os.posix_fadvise(target.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass  # purely an optimisation


class TooLargeError(Exception):
    pass


def guess_ext(url: str, default: str) -> str:
    m = re.search(
        r"\.(jpe?g|png|webp|heic|gif|mp4|mov|webm)(?:$|\?)", urllib.parse.urlsplit(url).path + "?", re.I
    )
    return m.group(1).lower() if m else default
