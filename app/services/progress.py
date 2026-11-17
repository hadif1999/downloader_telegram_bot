"""Live download progress: collects events from the download thread and renders the status card.

Platforms report progress through a plain callback, ``progress(done, total, **detail)``, from
the worker's download thread. `Monitor` records the latest state under a lock (cheap enough to
call for every received block) and the worker's event loop renders and edits the Telegram
message on its own schedule, so a slow Telegram edit never stalls the download.

Detail keys platforms may send (all optional):
    stage      "download" (default), "merge", "convert" or "finalize"
    track      "video" or "audio" while yt-dlp fetches the separate streams of one format
    part/parts current HLS/DASH segment (1-based) and the number of segments
    part_done/part_size  bytes of the current part received / its size (optional)
    connections  parallel connections of a segmented HTTP download
    retry/retries/reason/wait/kept  sent instead of bytes when an attempt failed and is retried
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from html import escape

from yt_dlp.utils import DownloadCancelled

from app.i18n import t
from app.services.captions import human_size

CELLS = 10
PART_CELLS = 6
SPEED_WINDOW = 12.0  # seconds of samples behind the displayed speed
STALL_NOTICE = 15  # seconds without data before the card says it is waiting
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
    """Compact human duration: 45s, 3m 05s, 1h 02m (in the current language)."""
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return t("{s}s", s=seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return t("{m}m {s:02}s", m=minutes, s=seconds)
    hours, minutes = divmod(minutes, 60)
    return t("{h}h {m:02}m", h=hours, m=minutes)


class Monitor:
    """Thread-safe progress state of one download request."""

    def __init__(self, unit: str = "bytes", now=time.monotonic, timeout: float | None = None) -> None:
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
        self.timeout = timeout  # stall timeout, shown while no data arrives
        self.last_data = self.started
        self.trouble: dict | None = None  # the retry in progress, until data flows again
        self.retries_used = 0

    # --- called from the download thread
    def __call__(self, done: int, total: int | None = None, **detail) -> None:
        if self.cancel.is_set():
            raise Cancelled
        stage = detail.pop("stage", "download")
        if "retry" in detail:
            with self._lock:
                self.done = max(0, done)
                self.detail = {
                    k: v for k, v in detail.items() if k.startswith("part") or k in ("connections", "track")
                }
                self._retrying("part", detail)
            return
        with self._lock:
            if stage != "download":
                self._enter(stage)
                return
            if self.stage != "download":
                self._enter("download")
            now = self._now()
            if done > self.done or self.stage == "prepare":
                self.last_data = now
                self.trouble = None  # data flows again: the retry worked
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
    def restarting(self, retry: int, retries: int, reason: str, wait: float) -> None:
        """The whole download failed and runs again; files already on disk are reused."""
        with self._lock:
            self._retrying("download", {"retry": retry, "retries": retries, "reason": reason, "wait": wait})

    def _retrying(self, scope: str, detail: dict) -> None:
        now = self._now()
        self.retries_used += 1
        self.trouble = {
            "scope": scope,
            "retry": detail["retry"],
            "retries": detail["retries"],
            "reason": detail.get("reason") or "a network error",
            "until": now + (detail.get("wait") or 0),
            "kept": detail.get("kept", self.done),
            "part": detail.get("part") if (detail.get("parts") or 0) > 1 else None,
        }
        self.last_data = now
        self._samples.clear()  # the stall would drag the speed down after resuming

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
            if self.trouble:
                body += "\n" + self._trouble_lines(now)
            footer = t("⏱ Elapsed {time}", time=clock(now - self.started))
            if self.cancel.is_set() and self.stage != "upload":
                footer += "\n" + t("🛑 <i>Cancelling…</i>")
            return f"{body}\n{footer}\n{self._steps()}"

    def _steps(self) -> str:
        current = {"prepare": "download", "download": "download", "upload": "upload"}.get(
            self.stage, "process"
        )
        order = [key for key, _ in STEPS]
        here = order.index(current)
        marks = []
        for index, (_, name) in enumerate(STEPS):
            icon = "✅" if index < here else "🔵" if index == here else "⚪"
            marks.append(f"{icon} {t(name)}")
        return "<i>" + " › ".join(marks) + "</i>"

    def _body(self, now: float) -> str:
        step = t("⚙️ Step time {time}", time=clock(now - self.stage_started))
        if self.stage in PROCESS_STAGES:
            icon, title, hint = PROCESS_STAGES[self.stage]
            return f"{icon} <b>{t(title)}</b>\n{t(hint)}\n{step}"
        if self.stage == "upload":
            size = f"📦 {human_size(self.size)}\n" if self.size else ""
            return (
                t("📤 <b>Uploading to Telegram</b>\nDownload complete. Sending your file…")
                + f"\n{size}{step}"
            )
        if self.stage == "prepare" or (not self.done and not self.total):
            return t("⏳ <b>Preparing download</b>\nWaiting for the first bytes…")
        if self.unit == "files":
            return self._files()
        return self._bytes()

    def _files(self) -> str:
        if not self.total:
            return t(
                "⬇️ <b>Downloading</b>\n<b>{n}</b> files downloaded\n<i>Total count not provided by the source.</i>",
                n=self.done,
            )
        lines = [
            t("⬇️ <b>Downloading</b>"),
            f"<code>{bar(self.done / self.total)}</code> <b>{int(self.done * 100 / self.total)}%</b>",
            t("🗂 <b>{done}</b> / {total} files", done=self.done, total=self.total),
        ]
        if self.done < self.total:
            lines.append(
                t("📥 Fetching file {n} of {total}", n=min(self.done + 1, self.total), total=self.total)
            )
        return "\n".join(lines)

    def _bytes(self) -> str:
        track = self.detail.get("track")
        title = t("⬇️ <b>Downloading</b>")
        if track in ("video", "audio"):
            title += " · " + t("video track" if track == "video" else "audio track")
        speed = self._speed()
        lines = [title]
        if self.total:
            pct = min(100, int(self.done * 100 / self.total))
            lines.append(f"<code>{bar(self.done / self.total)}</code> <b>{pct}%</b>")
            lines.append(
                f"📦 <b>{human_size(self.done) if self.done else '0B'}</b> / {human_size(self.total)}"
            )
        else:
            lines.append(t("📦 <b>{size}</b> received", size=human_size(self.done)))
            lines.append(t("<i>Total size not provided by the source.</i>"))
        if part_line := self._part_line():
            lines.append(part_line)
        if self.trouble:
            return "\n".join(lines)  # the retry notice replaces speed and ETA
        idle = self._now() - self.last_data
        if idle >= STALL_NOTICE:
            if self.timeout:
                lines.append(
                    t(
                        "⏸ No data for {idle}, retrying after {limit}",
                        idle=clock(idle),
                        limit=clock(self.timeout),
                    )
                )
            else:
                lines.append(t("⏸ No data for {idle}", idle=clock(idle)))
        else:
            rate = f"🚀 {human_size(int(speed))}/s" if speed else t("🚀 measuring speed…")
            if speed and self.total and self.total > self.done:
                rate += " · " + t("⏳ {time} left", time=clock((self.total - self.done) / speed))
            lines.append(rate)
        if self.retries_used:
            text = (
                "🔁 Recovered after {n} retry" if self.retries_used == 1 else "🔁 Recovered after {n} retries"
            )
            lines.append(t(text, n=self.retries_used))
        return "\n".join(lines)

    def _trouble_lines(self, now: float) -> str:
        trouble = self.trouble
        wait = trouble["until"] - now
        when = t("next try in {time}", time=clock(wait)) if wait >= 1 else t("reconnecting…")
        replied = re.fullmatch(r"the server replied (\d+)", trouble["reason"])
        reason = escape(t("the server replied {code}", code=replied[1]) if replied else t(trouble["reason"]))
        numbers = {"retry": trouble["retry"], "retries": trouble["retries"], "when": when}
        if trouble["scope"] == "download":
            lines = [
                t("⚠️ <b>Download interrupted</b>: {reason}.", reason=reason),
                t("🔁 Restarting, retry <b>{retry}</b> of {retries} · {when}", **numbers),
            ]
            if trouble["kept"]:
                lines.append(
                    t("♻️ {size} already downloaded will be reused", size=human_size(trouble["kept"]))
                )
            return "\n".join(lines)
        lines = [t("⚠️ <b>Connection problem</b>: {reason}.", reason=reason)]
        if trouble["part"]:
            lines.append(
                t(
                    "🔁 Retry <b>{retry}</b> of {retries} of part {part} · {when}",
                    part=trouble["part"],
                    **numbers,
                )
            )
        else:
            lines.append(t("🔁 Retry <b>{retry}</b> of {retries} · {when}", **numbers))
        if trouble["kept"]:
            lines.append(
                t("♻️ Resuming from {size}, nothing is downloaded twice", size=human_size(trouble["kept"]))
            )
        return "\n".join(lines)

    def _part_line(self) -> str | None:
        part, parts = self.detail.get("part"), self.detail.get("parts")
        if not part or not parts or parts < 2:
            connections = self.detail.get("connections") or 0
            if connections > 1:
                return t("🔗 {n} parallel connections", n=connections)
            return None
        line = t("🧩 Part <b>{part}</b> of {parts}", part=part, parts=parts)
        size, got = self.detail.get("part_size"), self.detail.get("part_done")
        if size and got is not None:
            line += f" <code>{bar(got / size, PART_CELLS)}</code> {min(100, int(got * 100 / size))}%"
        return line
