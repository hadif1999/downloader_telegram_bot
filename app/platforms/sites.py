"""Sites handled by yt-dlp, with gallery-dl / OpenGraph fallbacks for photo posts."""

from app.platforms.ytdlp import YtDlpPlatform


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
