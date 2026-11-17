"""Spotify: audio is DRM-protected, so match the track on YouTube (as spotDL does) and tag the mp3."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import get_settings
from app.models import CollectionEntry, DownloadResult, FormatOption, MediaInfo
from app.platforms.base import ProgressCb
from app.platforms.common import BROWSER_HEADERS, CRAWLER_UA, download_file, fetch, og_tags, url_key
from app.platforms.downloader import YoutubeDL
from app.platforms.network import on_domains
from app.platforms.youtube import YouTube
from app.platforms.ytdlp import UserError, YtDlpPlatform

LINK_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|album|playlist|episode|artist)/(\w+)", re.I)
SHORT_RE = re.compile(r"spotify\.link/\w+", re.I)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
TRACK_URI_RE = re.compile(r"spotify:track:(\w+)")


def parse_embed(page: str) -> dict:
    """The public embed page of an album/playlist carries its track list as Next.js page data:
    entity = {title, subtitle (artist/owner), trackList: [{uri, title, subtitle, duration (ms)}]}."""
    m = NEXT_DATA_RE.search(page)
    if not m:
        raise UserError("Couldn't read this Spotify album or playlist.")
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        raise UserError("Couldn't read this Spotify album or playlist.") from None
    if not isinstance(entity, dict):
        raise UserError("Couldn't read this Spotify album or playlist.")
    return entity


def embed_entries(entity: dict) -> list[CollectionEntry]:
    entries = []
    for track in entity.get("trackList") or []:
        m = TRACK_URI_RE.fullmatch(str((track or {}).get("uri") or ""))
        if not m or track.get("isPlayable") is False:
            continue  # podcast episodes and region-locked tracks
        duration = track.get("duration")
        entries.append(
            CollectionEntry(
                url=f"https://open.spotify.com/track/{m.group(1)}",
                title=" ".join(str(track.get("title") or "").split())[:200] or m.group(1),
                duration=int(duration) // 1000 if isinstance(duration, int | float) and duration else None,
                uploader=" ".join(str(track.get("subtitle") or "").split()) or None,
            )
        )
    return entries


def embed_cover(entity: dict) -> str | None:
    images = ((entity.get("visualIdentity") or {}).get("image")) or []
    images = [i for i in images if isinstance(i, dict) and i.get("url")]
    return max(images, key=lambda i: i.get("maxWidth") or 0)["url"] if images else None


def parse_track(tags: dict[str, str]) -> dict:
    """og:description looks like 'Rick Astley · Whenever You Need Somebody · Song · 1987'."""
    parts = [p.strip() for p in tags.get("og:description", "").split("·")]
    artist = parts[0] if parts else ""
    album = parts[1] if len(parts) > 3 else None
    year = parts[-1] if parts and re.fullmatch(r"\d{4}", parts[-1]) else None
    duration = tags.get("music:duration")
    return {
        "title": tags.get("og:title", ""),
        "artist": artist,
        "album": album,
        "year": year,
        "duration": int(duration) if duration and duration.isdigit() else None,
        "cover": tags.get("og:image"),
    }


def pick_match(entries: list[dict], duration: int | None) -> dict | None:
    """Closest duration among search results (within 15s when Spotify gives a duration)."""
    entries = [e for e in entries if e and e.get("url")]
    if not entries or not duration:
        return entries[0] if entries else None
    best = min(entries, key=lambda e: abs((e.get("duration") or 0) - duration))
    return best if abs((best.get("duration") or 0) - duration) <= 15 else entries[0]


def tag_mp3(path: Path, meta: dict, cover: Path | None) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=meta["title"]))
    tags.add(TPE1(encoding=3, text=meta["artist"]))
    if meta.get("album"):
        tags.add(TALB(encoding=3, text=meta["album"]))
    if meta.get("year"):
        tags.add(TDRC(encoding=3, text=meta["year"]))
    if cover and cover.exists():
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover.read_bytes()))
    tags.save(path)


COLLECTIONS = ("album", "playlist")


class Spotify(YtDlpPlatform):
    name, title, emoji = "sp", "Spotify", "🟢"

    def __init__(self) -> None:
        super().__init__()
        self._yt = YouTube()

    def match(self, url: str) -> str | None:
        if not on_domains(url, ("open.spotify.com", "spotify.link")):
            return None
        if m := LINK_RE.search(url):
            return m.group(2)
        return url_key(url) if SHORT_RE.search(url) else None  # spotify.link short link

    def extract(self, url: str) -> MediaInfo:
        page = None
        final = url
        if not (on_domains(url, ("open.spotify.com",)) and LINK_RE.search(url)):
            final, page = fetch(url, {"User-Agent": CRAWLER_UA})  # spotify.link short link
        m = LINK_RE.search(final)
        if (
            not on_domains(final, ("open.spotify.com",))
            or not m
            or m.group(1) not in COLLECTIONS + ("track",)
        ):
            raise UserError("Only Spotify tracks, albums and playlists are supported.")
        if m.group(1) in COLLECTIONS:
            return self.extract_collection(url, m.group(1), m.group(2))
        if page is None:
            final, page = fetch(final, {"User-Agent": CRAWLER_UA})
        meta = parse_track(og_tags(page))
        if not meta["title"]:
            raise UserError("Couldn't read this Spotify track.")
        return MediaInfo(
            platform=self.name,
            id=self.match(url) or m.group(2),
            url=f"https://open.spotify.com/track/{m.group(2)}",
            title=meta["title"],
            uploader=meta["artist"],
            duration=meta["duration"],
            upload_date=meta["year"],
            category=meta["album"],
            thumbnail=meta["cover"],
            formats=[FormatOption("mp3", "MP3", None)],
            kind="audio",
            extra=meta,
        )

    def extract_collection(self, url: str, kind: str, spotify_id: str) -> MediaInfo:
        _, page = fetch(f"https://open.spotify.com/embed/{kind}/{spotify_id}", BROWSER_HEADERS)
        entity = parse_embed(page)
        entries = embed_entries(entity)
        if not entries:
            raise UserError("No downloadable items found in this playlist.")
        return MediaInfo(
            platform=self.name,
            id=self.match(url) or spotify_id,
            url=f"https://open.spotify.com/{kind}/{spotify_id}",
            title=" ".join(str(entity.get("title") or entity.get("name") or "").split())[:200] or "Spotify",
            uploader=" ".join(str(entity.get("subtitle") or "").split()) or None,
            thumbnail=embed_cover(entity),
            kind="collection",
            entries=entries[: get_settings().collection_max_items],
            extra={"total": len(entries), "audio": True},
        )

    def find_on_youtube(self, meta: dict) -> str:
        query = f"ytsearch5:{meta['artist']} - {meta['title']} audio"
        with YoutubeDL({**self._yt.ydl_opts(), "extract_flat": True}) as ydl:
            res = ydl.extract_info(query, download=False) or {}
        match = pick_match(res.get("entries") or [], meta.get("duration"))
        if not match:
            raise UserError("Couldn't find this track on YouTube.")
        return match["url"]

    def download(
        self, info: MediaInfo, fmt: str, lang: str | None, dest: Path, progress: ProgressCb
    ) -> DownloadResult:
        source = self.find_on_youtube(info.extra)
        result = self._yt.download_url(source, info, "mp3", None, dest, progress)
        cover = None
        if info.thumbnail:
            try:
                cover = download_file(info.thumbnail, dest / "cover.jpg")
            except Exception:
                cover = None
        tag_mp3(Path(result.path), info.extra, cover)
        result.title, result.performer = info.extra["title"], info.extra["artist"]
        return result
