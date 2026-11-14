"""Sites handled by yt-dlp, with gallery-dl / OpenGraph fallbacks for photo posts."""

from pathlib import Path

from app.config import get_settings
from app.models import DownloadResult, GalleryItem
from app.platforms.network import on_domains
from app.platforms.ytdlp import TooLarge, UserError, YtDlpPlatform


class Instagram(YtDlpPlatform):
    name, title, emoji = "ig", "Instagram", "📸"
    domains = ("instagram.com",)
    patterns = (r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv|stories|share)/",)
    gallery = True  # photo posts and mixed carousels


class TikTok(YtDlpPlatform):
    name, title, emoji = "tt", "TikTok", "🎵"
    domains = ("tiktok.com",)
    patterns = (r"(?:^|[/.])tiktok\.com/",)
    gallery = True  # photo slideshows


class Pinterest(YtDlpPlatform):
    name, title, emoji = "pin", "Pinterest", "📌"
    domains = (
        "pinterest.com",
        "pinterest.co.uk",
        "pinterest.ca",
        "pinterest.de",
        "pinterest.fr",
        "pinterest.es",
        "pinterest.it",
        "pinterest.com.au",
        "pinterest.jp",
        "pin.it",
    )
    patterns = (r"pinterest\.[a-z.]+/pin/", r"pin\.it/")
    gallery = True  # image pins
    opengraph = True


class Snapchat(YtDlpPlatform):
    name, title, emoji = "sc", "Snapchat", "👻"
    domains = ("snapchat.com",)
    patterns = (r"snapchat\.com/",)
    gallery = True
    opengraph = True  # spotlight pages expose og:video
    use_ytdlp = False  # yt-dlp's SnapchatSpotlight extractor is broken (Nov 2026)


class Likee(YtDlpPlatform):
    name, title, emoji = "lk", "Likee", "💜"
    domains = ("likee.video", "likee.com")
    patterns = (r"likee\.(?:video|com)/",)
    opengraph = True


class VK(YtDlpPlatform):
    name, title, emoji = "vk", "VK", "🅥"
    domains = ("vk.com", "vk.ru", "vkvideo.ru")
    patterns = (r"(?:^|[/.])(?:vk\.com|vk\.ru|vkvideo\.ru)/",)
    gallery = True  # wall posts with photos


class Facebook(YtDlpPlatform):
    name, title, emoji = "fb", "Facebook", "📘"
    domains = ("facebook.com", "fb.watch", "fb.com")
    patterns = (r"(?:^|[/.])(?:facebook\.com|fb\.watch|fb\.com)/",)
    gallery = True  # photo posts
    opengraph = True


class SoundCloud(YtDlpPlatform):
    name, title, emoji = "scl", "SoundCloud", "☁️"
    domains = ("soundcloud.com",)
    patterns = (r"(?:^|[/.])soundcloud\.com/",)

    def ydl_opts(self):
        return {**super().ydl_opts(), "noplaylist": False, "extract_flat": "in_playlist", "playlistend": 51}

    def playlist_info(self, info, url, media_id):
        entries = list(info.get("entries") or [])
        if len(entries) > 50:
            raise UserError("SoundCloud sets support up to 50 tracks. Please send individual track links.")
        if not entries:
            raise UserError("No downloadable tracks found in this SoundCloud set.")
        items = []
        for entry in entries:
            track_url = (entry or {}).get("webpage_url") or (entry or {}).get("url")
            if not track_url or not on_domains(track_url, self.domains):
                raise UserError("Couldn't read every track in this SoundCloud set. Please try again later.")
            items.append(GalleryItem(track_url, "audio"))
        result = self.to_media_info(info, url, media_id)
        result.kind, result.items, result.formats = "gallery", items, []
        return result

    def download(self, info, fmt, lang, dest, progress):
        if info.kind != "gallery":
            return super().download(info, fmt, lang, dest, progress)
        files = []
        for index, item in enumerate(info.items):
            progress(index, len(info.items))
            track = self.extract(item.url)
            if track.kind != "audio":
                raise UserError("A track in this SoundCloud set is no longer available.")
            track_dest = dest / str(index)
            track_dest.mkdir(parents=True, exist_ok=True)
            result = self.download_url(item.url, track, "mp3", None, track_dest, lambda *_: None)
            if Path(result.path).stat().st_size > get_settings().max_file_bytes:
                raise TooLarge(track.id)
            files.append((result.path, "audio"))
        progress(len(files), len(files))
        return DownloadResult(files=files)
