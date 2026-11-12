from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class FormatOption:
    key: str  # "1080", "720", ..., "mp3"
    label: str  # "1080p", "MP3"
    size: int | None  # estimated bytes
    height: int | None = None


@dataclass
class AudioTrack:
    lang: str  # yt-dlp language code, e.g. "de-DE"
    label: str  # button label, e.g. "DE-DE"
    original: bool = False


@dataclass
class GalleryItem:
    url: str
    type: str = "photo"  # "photo" | "video"


@dataclass
class MediaInfo:
    platform: str
    id: str
    url: str
    title: str
    uploader: str | None = None
    uploader_url: str | None = None
    uploader_handle: str | None = None
    subscribers: int | None = None
    duration: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    category: str | None = None
    upload_date: str | None = None  # YYYY-MM-DD
    thumbnail: str | None = None
    description: str = ""
    formats: list[FormatOption] = field(default_factory=list)
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    # "video" (resolution menu), "audio" (mp3 only) or "gallery" (photos/videos sent as an album)
    kind: str = "video"
    items: list[GalleryItem] = field(default_factory=list)  # gallery entries
    extra: dict = field(default_factory=dict)  # platform-specific data (e.g. direct media url)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MediaInfo:
        d = dict(d)
        d["formats"] = [FormatOption(**f) for f in d.get("formats", [])]
        d["audio_tracks"] = [AudioTrack(**a) for a in d.get("audio_tracks", [])]
        d["items"] = [GalleryItem(**i) for i in d.get("items", [])]
        return cls(**d)

    def format(self, key: str) -> FormatOption | None:
        return next((f for f in self.formats if f.key == key), None)


@dataclass
class DownloadResult:
    path: str = ""
    # gallery downloads: (local path, "photo" | "video") in post order
    files: list[tuple[str, str]] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    title: str | None = None
    performer: str | None = None
