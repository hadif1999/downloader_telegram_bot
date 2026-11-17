"""Live download progress: collects events from the download thread and renders the status card.

Platforms report progress through a plain callback, ``progress(done, total, **detail)``, from
the worker's download thread. `Monitor` records the latest state under a lock (cheap enough to
call for every received block) and the worker's event loop renders and edits the Telegram
message on its own schedule, so a slow Telegram edit never stalls the download.

Detail keys platforms may send (all optional):
    stage      "download" (default), "merge", "convert" or "finalize"
    track      "video" or "audio" while yt-dlp fetches the separate streams of one format
    part/parts current byte-range part (1-based) and the number of parts
    part_done/part_size  bytes of the current part received / its size
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from html import escape

from yt_dlp.utils import DownloadCancelled

from app.services.captions import human_size

CELLS = 10
PART_CELLS = 6
SPEED_WINDOW = 12.0  # seconds of samples behind the displayed speed
STEPS = (("queue", "Queue"), ("download", "Download"), ("process", "Process"), ("upload", "Upload"))
PROCESS_STAGES = {
    "merge": ("🎞", "Merging video and audio", "Combining both tracks into one MP4 file…"),
    "convert": ("🎵", "Converting to MP3", "Extracting the audio track at 192 kbps…"),
    "finalize": ("🏷", "Adding tags and cover art", "Almost done…"),
}


class Cancelled(DownloadCancelled):
    """The user cancelled the download. yt-dlp lets this type propagate out of its hooks."""

    def __init__(self) -> None:
        super().__init__("Download cancelled by the user")


def bar(fraction: float, cells: int = CELLS) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * cells)
    return "▓" * filled + "░" * (cells - filled)


def clock(seconds: float | None) -> str:
    """Compact human duration: 45s, 3m 05s, 1h 02m."""
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02}m"


class Monitor:
    """Thread-safe progress state of one download request."""

    def __init__(self, unit: str = "bytes", now=time.monotonic) -> None:
        self.unit = unit
        self._now = now
        self._lock = threading.Lock()
        self.cancel = threading.Event()  # set from the event loop when the user presses Cancel
        self.stage = "prepare"
        self.done = 0
        self.total: int | None = None
        self.detail: dict = {}
        self.size: int | None = None  # final file size, shown while uploading
        self.started = now()
        self.stage_started = self.started
        self._samples: deque[tuple[float, int]] = deque()

    # --- called from the download thread
    def __call__(self, done: int, total: int | None = None, **detail) -> None:
        if self.cancel.is_set():
            raise Cancelled
        stage = detail.pop("stage", "download")
        with self._lock:
            if stage != "download":
                self._enter(stage)
                return
            if self.stage != "download":
                self._enter("download")
            now = self._now()
            self.done, self.total = max(0, done), total if total and total > 0 else None
            self.detail = detail
            if self.unit == "bytes":
                if self._samples and done < self._samples[-1][1]:
                    self._samples.clear()  # counter restarted (retry): old samples are meaningless
                if not self._samples or now - self._samples[-1][0] >= 0.5:
                    self._samples.append((now, done))
                while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW:
                    self._samples.popleft()

    # --- called from the event loop
    def set_stage(self, stage: str, size: int | None = None) -> None:
        with self._lock:
            self._enter(stage)
            if size is not None:
                self.size = size

    def _enter(self, stage: str) -> None:
        if stage != self.stage:
            self.stage, self.stage_started = stage, self._now()

    @property
    def cancellable(self) -> bool:
        return self.stage not in {"upload", "cancelled"} and not self.cancel.is_set()

    def speed(self) -> float | None:
        with self._lock:
            return self._speed()

    def _speed(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 - t0 < 1.0 or d1 <= d0:
            return None
        return (d1 - d0) / (t1 - t0)

    def render(self) -> str:
        with self._lock:
            now = self._now()
            body = self._body(now)
            footer = f"⏱ Elapsed {clock(now - self.started)}"
            if self.cancel.is_set() and self.stage != "upload":
                footer += "\n🛑 <i>Cancelling…</i>"
            return f"{body}\n{footer}\n{self._steps()}"

    def _steps(self) -> str:
        current = {"prepare": "download", "download": "download", "upload": "upload"}.get(
            self.stage, "process"
        )
        order = [key for key, _ in STEPS]
        here = order.index(current)
        marks = []
        for index, (_, label) in enumerate(STEPS):
            icon = "✅" if index < here else "🔵" if index == here else "⚪"
            marks.append(f"{icon} {label}")
        return "<i>" + " › ".join(marks) + "</i>"

    def _body(self, now: float) -> str:
        if self.stage in PROCESS_STAGES:
            icon, title, hint = PROCESS_STAGES[self.stage]
            return f"{icon} <b>{title}</b>\n{hint}\n⚙️ Step time {clock(now - self.stage_started)}"
        if self.stage == "upload":
            size = f"📦 {human_size(self.size)}\n" if self.size else ""
            return (
                "📤 <b>Uploading to Telegram</b>\nDownload complete. Sending your file…\n"
                f"{size}⚙️ Step time {clock(now - self.stage_started)}"
            )
        if self.stage == "prepare" or (not self.done and not self.total):
            return "⏳ <b>Preparing download</b>\nWaiting for the first bytes…"
        if self.unit == "files":
            return self._files()
        return self._bytes()

    def _files(self) -> str:
        if not self.total:
            return f"⬇️ <b>Downloading</b>\n<b>{self.done}</b> files downloaded\n<i>Total count not provided by the source.</i>"
        current = min(self.done + 1, self.total)
        lines = [
            "⬇️ <b>Downloading</b>",
            f"<code>{bar(self.done / self.total)}</code> <b>{int(self.done * 100 / self.total)}%</b>",
            f"🗂 <b>{self.done}</b> / {self.total} files",
        ]
        if self.done < self.total:
            lines.append(f"📥 Fetching file {current} of {self.total}")
        return "\n".join(lines)

    def _bytes(self) -> str:
        track = self.detail.get("track")
        title = "⬇️ <b>Downloading</b>" + (f" · {escape(track)} track" if track else "")
        speed = self._speed()
        lines = [title]
        if self.total:
            pct = min(100, int(self.done * 100 / self.total))
            lines.append(f"<code>{bar(self.done / self.total)}</code> <b>{pct}%</b>")
            lines.append(
                f"📦 <b>{human_size(self.done) if self.done else '0B'}</b> / {human_size(self.total)}"
            )
        else:
            lines.append(f"📦 <b>{human_size(self.done)}</b> received")
            lines.append("<i>Total size not provided by the source.</i>")
        if part_line := self._part_line():
            lines.append(part_line)
        rate = f"🚀 {human_size(int(speed))}/s" if speed else "🚀 measuring speed…"
        if speed and self.total and self.total > self.done:
            rate += f" · ⏳ {clock((self.total - self.done) / speed)} left"
        lines.append(rate)
        return "\n".join(lines)

    def _part_line(self) -> str | None:
        part, parts = self.detail.get("part"), self.detail.get("parts")
        if not part or not parts or parts < 2:
            return None
        line = f"🧩 Part <b>{part}</b> of {parts}"
        size, got = self.detail.get("part_size"), self.detail.get("part_done")
        if size and got is not None:
            line += f" <code>{bar(got / size, PART_CELLS)}</code> {min(100, int(got * 100 / size))}%"
        return line
