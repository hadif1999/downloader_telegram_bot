"""Small HTTP / parsing helpers shared by platforms."""

from __future__ import annotations

import hashlib
import html
import os
import re
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
PART_RETRIES = 3
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)", re.I)


def download_file(
    url: str,
    dest: Path,
    referer: str | None = None,
    timeout: int = 120,
    progress=None,
    max_bytes: int | None = None,
) -> Path:
    """HTTP download in parts of DOWNLOAD_PART_MB, streamed straight into `dest`.

    Each part is a Range request written at its offset in the final file (no separate part files
    and no join step, so disk use never doubles). Every FLUSH_BYTES the data is flushed to disk and
    dropped from the page cache, which keeps the memory charged to the container flat (~16 MB of
    cache) even for multi-gigabyte files. A failed part is retried on its own instead of
    restarting the download. Servers without Range support are streamed in one response with the
    same periodic flushing.
    progress(done_bytes, total_bytes_or_None) is called per 1 MB chunk."""
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    s = get_settings()
    limit = s.max_file_bytes if max_bytes is None else max_bytes
    part = s.download_part_bytes
    with open(dest, "wb") as f:
        done, total, ranged = _first_part(url, headers, timeout, f, part, limit, progress)
        while ranged and total is not None and done < total:
            end = min(done + part, total) - 1
            for attempt in range(PART_RETRIES):
                try:
                    _ranged_part(url, headers, timeout, f, done, end, total, progress)
                    break
                except OSError:  # includes HTTP and connection errors for this part only
                    if attempt == PART_RETRIES - 1:
                        raise
                    f.seek(done)
                    f.truncate()  # discard the incomplete part and fetch it again
            done = end + 1
            release_page_cache(f)
        if total is not None and done != total:
            raise ConnectionError(f"Incomplete download: {done} of {total} bytes")
    return dest


def _first_part(url, headers, timeout, f, part, limit, progress) -> tuple[int, int | None, bool]:
    """Request the first part. Returns (bytes written, total size or None, server supports ranges)."""
    req = urllib.request.Request(url, headers={**headers, "Range": f"bytes=0-{part - 1}"})
    try:
        response = open_url(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code != 416:  # 416: e.g. an empty resource; retry as a plain request
            raise
        response = open_url(urllib.request.Request(url, headers=headers), timeout=timeout)
    with response as r:
        match = CONTENT_RANGE.fullmatch(r.headers.get("Content-Range") or "")
        if getattr(r, "status", 200) == 206 and match and int(match.group(1)) == 0:
            total = None if match.group(3) == "*" else int(match.group(3))
            if total is not None and total > limit:
                raise TooLargeError(url)
            expected = int(match.group(2)) + 1
            done = _copy(r, f, 0, total, limit, progress, url)
            if done != expected:
                raise ConnectionError(f"Short part: {done} of {expected} bytes")
            release_page_cache(f)
            return done, total, total is not None
        # No Range support: stream the whole body, still flushing every part.
        total = int(r.headers.get("Content-Length") or 0) or None
        if total and total > limit:
            raise TooLargeError(url)
        done = _copy(r, f, 0, total, limit, progress, url)
        release_page_cache(f)
        return done, total, False


def _ranged_part(url, headers, timeout, f, start, end, total, progress) -> None:
    req = urllib.request.Request(url, headers={**headers, "Range": f"bytes={start}-{end}"})
    with open_url(req, timeout=timeout) as r:
        match = CONTENT_RANGE.fullmatch(r.headers.get("Content-Range") or "")
        if getattr(r, "status", 200) != 206 or not match or int(match.group(1)) != start:
            raise ConnectionError("Server stopped honouring byte ranges mid-download")
        f.seek(start)
        done = _copy(r, f, start, total, total, progress, url)
        if done != end + 1:
            raise ConnectionError(f"Short part: {done - start} of {end - start + 1} bytes")


def _copy(r, f, done: int, total, limit: int, progress, url: str) -> int:
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
            release_page_cache(f)
            since_flush = 0
    return done


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
