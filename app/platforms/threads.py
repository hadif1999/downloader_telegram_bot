"""Threads: no yt-dlp/gallery-dl support, so read the post JSON embedded in the page."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime

from app.models import FormatOption, GalleryItem, MediaInfo
from app.platforms.common import BROWSER_HEADERS, fetch
from app.platforms.network import on_domains
from app.platforms.ytdlp import UserError, YtDlpPlatform

POST_RE = re.compile(r"threads\.(?:net|com)/(?:@[\w.]+/post|t)/([\w-]+)", re.I)
BLOB_RE = re.compile(r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', re.S)


def _posts(obj) -> Iterator[dict]:
    if isinstance(obj, dict):
        if "code" in obj and "media_type" in obj:
            yield obj
        for v in obj.values():
            yield from _posts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _posts(v)


def find_post(page: str, code: str) -> dict | None:
    for blob in BLOB_RE.findall(page):
        if code not in blob:
            continue
        for post in _posts(json.loads(blob)):
            if post.get("code") == code:
                return post
    return None


def _media(m: dict) -> GalleryItem | None:
    if videos := m.get("video_versions"):
        return GalleryItem(videos[0]["url"], "video")
    if cands := (m.get("image_versions2") or {}).get("candidates"):
        best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        return GalleryItem(best["url"], "photo")
    return None


def post_to_media_info(post: dict, url: str) -> MediaInfo:
    media = post.get("carousel_media") or [post]
    items = [i for m in media if (i := _media(m))]
    if not items:
        raise UserError("This Threads post has no photos or videos.")
    caption = ((post.get("caption") or {}).get("text") or "").strip()
    user = post.get("user") or {}
    thumb = _media({"image_versions2": post.get("image_versions2")})
    info = MediaInfo(
        platform="th",
        id=post["code"],
        url=url,
        title=(caption.split("\n")[0] or f"Threads post by @{user.get('username', '?')}")[:200],
        description=caption,
        uploader=user.get("full_name") or user.get("username"),
        uploader_handle=f"@{user['username']}" if user.get("username") else None,
        uploader_url=f"https://www.threads.com/@{user['username']}" if user.get("username") else None,
        likes=post.get("like_count"),
        upload_date=datetime.fromtimestamp(post["taken_at"], UTC).strftime("%Y-%m-%d")
        if post.get("taken_at")
        else None,
        thumbnail=thumb.url if thumb else (items[0].url if items[0].type == "photo" else None),
    )
    if len(items) == 1 and items[0].type == "video":
        info.extra["direct_url"] = items[0].url
        info.duration = int(post["video_duration"]) if post.get("video_duration") else None
        info.formats = [FormatOption("best", "Video", None), FormatOption("mp3", "MP3", None)]
    else:
        info.kind, info.items = "gallery", items
    return info


class Threads(YtDlpPlatform):
    name, title, emoji = "th", "Threads", "🧵"

    def match(self, url: str) -> str | None:
        if not on_domains(url, ("threads.net", "threads.com")):
            return None
        m = POST_RE.search(url)
        return m.group(1) if m else None

    def extract(self, url: str) -> MediaInfo:
        code = self.match(url)
        page_url = re.sub(r"threads\.net", "threads.com", url, flags=re.I)
        for _ in range(3):  # the logged-out page doesn't always embed the post
            final, page = fetch(page_url, BROWSER_HEADERS)
            if code and (post := find_post(page, code)):
                return post_to_media_info(post, url)
        raise UserError("Couldn't read this Threads post (it may be private or need login).")
