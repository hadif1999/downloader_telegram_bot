"""Small HTTP / parsing helpers shared by platforms."""

from __future__ import annotations

import hashlib
import html
import re
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


def download_file(
    url: str,
    dest: Path,
    referer: str | None = None,
    timeout: int = 120,
    progress=None,
    max_bytes: int | None = None,
) -> Path:
    """Plain HTTP download; progress(done_bytes, total_bytes_or_None) is called per 1 MB chunk."""
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    limit = get_settings().max_file_bytes if max_bytes is None else max_bytes
    with open_url(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) or None
        if total and total > limit:
            raise TooLargeError(url)
        while chunk := r.read(1 << 20):
            f.write(chunk)
            if f.tell() > limit:
                raise TooLargeError(url)
            if progress:
                progress(f.tell(), total)
    return dest


class TooLargeError(Exception):
    pass


def guess_ext(url: str, default: str) -> str:
    m = re.search(
        r"\.(jpe?g|png|webp|heic|gif|mp4|mov|webm)(?:$|\?)", urllib.parse.urlsplit(url).path + "?", re.I
    )
    return m.group(1).lower() if m else default
