"""Photo posts / carousels: gallery-dl for extraction, plain HTTP (or yt-dlp) for downloading."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.models import DownloadResult, GalleryItem, MediaInfo
from app.platforms.base import ProgressCb
from app.platforms.common import download_file, guess_ext
from app.platforms.downloader import YoutubeDL, writable_cookies

VIDEO_EXT = {"mp4", "mov", "webm", "m4v"}
PHOTO_EXT = {"jpg", "jpeg", "png"}  # what Telegram accepts as a photo; others are converted
MAX_ITEMS = 50


def parse_dump(raw: str) -> tuple[dict, list[GalleryItem]]:
    """Parse `gallery-dl -j` output: [[type, url_or_meta, meta], ...] (3 = file url, -1 = error)."""
    meta: dict = {}
    items: list[GalleryItem] = []
    for msg in json.loads(raw or "[]"):
        if msg[0] == -1:
            raise RuntimeError(msg[1].get("message", "gallery-dl error"))
        if msg[0] == 2 and not meta:
            meta = msg[1]
        elif msg[0] == 3:
            url, kw = msg[1], msg[2]
            meta = meta or kw
            ext = (kw.get("extension") or guess_ext(url, "jpg")).lower()
            kind = "video" if ext in VIDEO_EXT or url.startswith("ytdl:") else "photo"
            items.append(GalleryItem(url, kind))
    return meta, items[:MAX_ITEMS]


def extract(url: str, cookies: Path | None = None, timeout: int = 60) -> tuple[dict, list[GalleryItem]]:
    cmd = [sys.executable, "-m", "app.platforms.gallery_cli", "-j", "--range", f"1-{MAX_ITEMS}"]
    with writable_cookies(cookies) as copy:
        if copy:
            cmd += ["--cookies", str(copy)]
        out = subprocess.run([*cmd, url], capture_output=True, text=True, timeout=timeout, check=True)
    return parse_dump(out.stdout)


def _first(meta: dict, *keys: str) -> Any:
    for k in keys:
        v = meta.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("username") or v.get("text")
        if v:
            return v
    return None


def to_media_info(
    platform: str, media_id: str, url: str, site: str, meta: dict, items: list[GalleryItem]
) -> MediaInfo:
    text = _first(meta, "description", "caption", "content", "title", "desc") or site
    date = _first(meta, "date", "post_date", "created_at")
    return MediaInfo(
        platform=platform,
        id=media_id,
        url=url,
        title=str(text).strip().split("\n")[0][:200],
        description=str(text),
        uploader=_first(meta, "fullname", "username", "author", "owner", "user", "nickname"),
        likes=_first(meta, "likes", "like_count", "digg_count"),
        upload_date=str(date)[:10] if date else None,
        thumbnail=next((i.url for i in items if i.type == "photo"), None),
        kind="gallery",
        items=items,
    )


def _to_jpeg(path: Path) -> Path:
    out = path.with_suffix(".jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-q:v", "2", str(out)],
        check=True,
        timeout=60,
    )
    path.unlink(missing_ok=True)
    return out


def download(
    items: list[GalleryItem], dest: Path, progress: ProgressCb, ydl_opts: dict, referer: str | None = None
) -> DownloadResult:
    dest.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, str]] = []
    for i, item in enumerate(items):
        progress(i, len(items))
        if item.url.startswith("ytdl:"):  # gallery-dl delegates some videos to yt-dlp
            opts = {**ydl_opts, "outtmpl": str(dest / f"{i:02}.%(ext)s"), "merge_output_format": "mp4"}
            with YoutubeDL(opts) as ydl:
                r = ydl.extract_info(item.url[5:], download=True) or {}
            path = Path(r["requested_downloads"][0]["filepath"])
        else:
            ext = guess_ext(item.url, "mp4" if item.type == "video" else "jpg")
            path = download_file(item.url, dest / f"{i:02}.{ext}", referer=referer)
            if item.type == "photo" and ext not in PHOTO_EXT:
                path = _to_jpeg(path)
        files.append((str(path), item.type))
    progress(len(items), len(items))
    return DownloadResult(files=files)
