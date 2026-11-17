"""Adaptive segmented HTTP downloads, in the manner of IDM and aria2.

One file is fetched over several connections, each writing its own byte range straight into
the final file with ``os.pwrite`` (no part files, no join step, disk use never doubles):

- **Probe**: the first ranged request reveals the size, Range support and a validator, and its
  body is the first chunk of connection 1, so a photo or a thumbnail costs one request as before.
  Servers without ranges or a known size raise `NotSegmentable`; the caller falls back.
- **Ramp-up**: connections are added one at a time, about every second, while each one still
  raises the total speed by MIN_GAIN, up to DOWNLOAD_CONNECTIONS. A connection the server
  refuses (403/429/503) lowers the limit for this download instead of failing it.
- **Dynamic segmentation** (IDM): a connection that finishes its range takes half of the
  largest remaining one, as long as both halves stay at least DOWNLOAD_MIN_SPLIT_MB. Small files
  therefore never split, and fast connections end up doing most of the work.
- **Slow connections** (aria2's lowest-speed-limit): one running below SLOW_RATIO of the median
  for SLOW_FOR seconds hands half of its range to a new connection, or reconnects.
- **Adaptive request size**: each connection asks for ranges that double while the speed holds
  and halve when it drops or a request fails, between MIN_CHUNK and the chunk cap (10 MB for
  YouTube, whose large ranges stall).
- **Resume**: the segment table is saved in ``<file>.segments.json`` (aria2's control file), so a
  re-run continues every range where it stopped; the file is deleted when the download completes.

Progress, retries and cancellation keep the contracts of `common.download_file`: progress is
reported from the calling thread only, and an exception raised by it (``Cancelled``) stops every
connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.platforms import common

log = logging.getLogger(__name__)

MIN_CHUNK = 1 << 20
FIRST_CHUNK = 4 << 20
# Hosts whose large ranges stall. YouTube serves ~1 MB of a large range and then nearly stops
# (measured on the VPS: 200 MB ranges 0.04 MB/s, 10 MB ranges 21-23 MB/s).
CHUNK_CAPS = {"googlevideo.com": 10 << 20}
TICK = 0.1  # coordinator loop period (it also wakes up on every worker event)
REPORT_EVERY = 0.5  # seconds between progress reports
RAMP_EVERY = 1.0  # seconds between added connections, and the window the speed is measured over
MIN_GAIN = 0.1  # stop adding connections once one adds less than 10 % speed
SAVE_EVERY = 2.0  # seconds between segment-table saves
SLOW_RATIO, SLOW_FOR, SLOW_WINDOW = 0.25, 15.0, 5.0
JOIN_GRACE = 2.0  # seconds to wait for each connection after a stop
REFUSED = {403, 429, 503}  # a server refusing *another* connection, not the download

ProgressCb = Callable[..., None]
RetryCb = Callable[[BaseException, int, int, float, int], None]


class NotSegmentable(Exception):
    """No byte ranges or no known size: the caller downloads in one stream instead."""


class _Complete(Exception):
    """Internal: the file on disk already has every byte (a re-run after the download finished)."""


class _Reconnect(Exception):
    """Internal: drop a slow connection and request the rest of its range again."""


def _pause(event: threading.Event, seconds: float) -> None:
    event.wait(seconds)  # a stop (cancel, error) interrupts the back-off


def status_of(error: BaseException) -> int | None:
    """HTTP status of a urllib or yt-dlp HTTP error, else None."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code
    status = getattr(error, "status", None) if hasattr(error, "response") else None
    return status if isinstance(status, int) else None


def chunk_cap_for(url: str, cap: int) -> int:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for suffix, limit in CHUNK_CAPS.items():
        if host == suffix or host.endswith("." + suffix):
            cap = min(cap, limit)
    return max(MIN_CHUNK, cap)


@dataclass
class Segment:
    start: int
    end: int  # exclusive
    done: int  # bytes [start, done) are on disk
    owner: int | None = None

    @property
    def remaining(self) -> int:
        return self.end - self.done


class Chunker:
    """Size of a connection's next range request: grows while the speed holds, like TCP slow start."""

    def __init__(self, cap: int) -> None:
        self.cap = max(MIN_CHUNK, cap)
        self.size = min(FIRST_CHUNK, self.cap)
        self.rate: float | None = None

    def measured(self, nbytes: int, seconds: float) -> None:
        if nbytes < self.size:  # cut short by the segment end: not a fair sample
            return
        rate = nbytes / max(seconds, 1e-6)
        if self.rate is not None:
            if rate >= 0.9 * self.rate:
                self.size = min(self.size * 2, self.cap)
            elif rate < 0.5 * self.rate:
                self.size = max(self.size // 2, MIN_CHUNK)
        self.rate = rate

    def failed(self) -> None:
        self.size, self.rate = max(self.size // 2, MIN_CHUNK), None


class Ramp:
    """Adds connections one at a time while each one still makes the download faster."""

    def __init__(self, now: float) -> None:
        self.last, self.before, self.plateau = now, None, False

    def should_add(self, now: float, rate: float, conns: int, max_conns: int) -> bool:
        if self.plateau or conns >= max_conns or now - self.last < RAMP_EVERY:
            return False
        if self.before is not None and self.before > 0 and rate < self.before * (1 + MIN_GAIN):
            self.plateau = True  # the server (or the link) is the bottleneck now
            return False
        self.before, self.last = rate, now
        return True


@dataclass
class Conn:
    wid: int
    primary: bool  # the download's own connection: a refusal is an error, not a limit
    bytes: int = 0
    seg: Segment | None = None
    reconnect: bool = False
    slow_since: float | None = None
    marks: deque = field(default_factory=deque)  # (time, bytes) samples for its speed


class SegmentedDownload:
    def __init__(
        self,
        url: str,
        dest: Path,
        opener: Callable,
        *,
        headers: dict,
        timeout: float,
        limit: int,
        retries: int,
        max_conns: int,
        min_split: int,
        chunk_cap: int,
        progress: ProgressCb | None = None,
        on_retry: RetryCb | None = None,
    ) -> None:
        self.url, self.dest, self.opener = url, Path(dest), opener
        self.headers, self.timeout, self.limit, self.retries = dict(headers), timeout, limit, retries
        self.max_conns, self.min_split = max(1, max_conns), max(MIN_CHUNK, min_split)
        self.chunk_cap = chunk_cap_for(url, chunk_cap)
        self.progress = progress or (lambda *a, **k: None)
        self.on_retry = on_retry or (lambda *a: None)
        self.state_path = self.dest.with_name(self.dest.name + ".segments.json")
        self.lock = threading.Lock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.segments: list[Segment] = []
        self.conns: dict[int, Conn] = {}
        self.threads: list[threading.Thread] = []
        self.retry_events: list[tuple[BaseException, int, int]] = []
        self.error: BaseException | None = None
        self.total = 0
        self.validator: str | None = None
        self.fd = -1
        self._next_wid = 0

    # ------------------------------------------------------------------ public
    def run(self) -> int:
        """Download the file; returns its size. Raises NotSegmentable before writing anything."""
        segments = self._load_state()
        if segments is not None and all(s.remaining <= 0 for s in segments):
            self._finish_file()
            return self.total
        first = next((s for s in segments or [] if s.remaining > 0), None)
        offset = first.done if first else (self.dest.stat().st_size if self.dest.exists() else 0)
        saved_total = self.total
        try:
            response, end = self._probe(offset)
            if segments is not None and self.total != saved_total:
                response.close()  # the file changed on the server: start over
                log.info("%s changed on the server: starting over", self.dest.name)
                self._reset_file()
                segments, offset = None, 0
                response, end = self._probe(0)
        except _Complete:
            with contextlib.suppress(OSError):
                os.truncate(self.dest, self.total)  # a resumed file may hold stale bytes past the end
            self._finish_file()
            return self.total
        except NotSegmentable:
            if segments is not None:
                self._collapse_to_prefix(segments)  # leave a file the plain downloader can resume
            raise
        if segments is None or first is None:  # fresh, or a partial file of a single-stream download
            segments = [Segment(0, self.total, offset)]
            first = segments[0]
        self.segments = segments
        self._save_state()  # before preallocating: a sized file without a table is never trusted
        mode = os.O_RDWR | os.O_CREAT
        self.fd = os.open(self.dest, mode, 0o644)
        try:
            with contextlib.suppress(OSError, AttributeError):
                os.posix_fallocate(self.fd, 0, self.total)  # also fails fast on a full disk
            if os.fstat(self.fd).st_size < self.total:
                os.ftruncate(self.fd, self.total)
            self._spawn(first, primary=True, response=(response, end))
            self._coordinate()
            self._finish_file()
        finally:
            with self.lock:
                self.stop.set()
                fd, self.fd = self.fd, -1
            if fd >= 0:
                os.close(fd)
        return self.total

    # ------------------------------------------------------------------ state file
    def _load_state(self) -> list[Segment] | None:
        if not self.state_path.exists():
            return None
        total, segments = 0, []
        try:
            state = json.loads(self.state_path.read_text())
            total = int(state["total"])
            segments = [Segment(int(a), int(b), int(c)) for a, b, c in state["segments"]]
            valid = bool(segments) and (
                self.dest.exists()
                and self.dest.stat().st_size == total
                and all(s.start <= s.done <= s.end <= total for s in segments)
            )
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            log.info("discarding an unusable segment table: %s", self.state_path.name)
            self._reset_file()
            return None
        self.total = total
        return sorted(segments, key=lambda s: s.start)

    def _save_state(self) -> None:
        with self.lock:
            data = {"total": self.total, "segments": [[s.start, s.end, s.done] for s in self.segments]}
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.state_path)
        except OSError:
            log.warning("couldn't save the segment table of %s", self.dest.name)

    def _reset_file(self) -> None:
        with contextlib.suppress(OSError):
            self.state_path.unlink()
        with contextlib.suppress(OSError):
            self.dest.unlink()

    def _collapse_to_prefix(self, segments: list[Segment]) -> None:
        """Keep only the contiguous bytes from 0 so a single-stream download can resume them."""
        prefix = 0
        for s in sorted(segments, key=lambda s: s.start):
            if s.start != prefix:
                break
            prefix = s.done
            if s.done < s.end:
                break
        with contextlib.suppress(OSError):
            os.truncate(self.dest, prefix)
        with contextlib.suppress(OSError):
            self.state_path.unlink()

    def _finish_file(self) -> None:
        common.release_page_cache(self.dest)  # once per file: all on disk, nothing left cached
        with contextlib.suppress(OSError):
            self.state_path.unlink()
        self.progress(self.total, self.total, connections=0)

    # ------------------------------------------------------------------ requests
    def _request(self, start: int, end: int):
        headers = {**self.headers, "Range": f"bytes={start}-{end}"}
        if self.validator:
            headers["If-Range"] = self.validator
        return self.opener(self.url, headers, self.timeout)

    def _probe(self, offset: int):
        """First ranged request: size, Range support and validator. Retries network errors."""
        attempt = 0
        while True:
            try:
                response = self.opener(
                    self.url,
                    {**self.headers, "Range": f"bytes={offset}-{offset + FIRST_CHUNK - 1}"},
                    self.timeout,
                )
            except Exception as error:
                if status_of(error) == 416:
                    headers = getattr(error, "headers", None) or getattr(
                        getattr(error, "response", None), "headers", None
                    )
                    whole = common.CONTENT_RANGE.fullmatch((headers or {}).get("Content-Range") or "")
                    if offset and whole and whole.group(3) != "*" and int(whole.group(3)) == offset:
                        self.total = offset
                        raise _Complete from error
                    raise NotSegmentable("range not satisfiable") from error
                attempt = self._retry_or_raise(error, attempt)
                continue
            match = common.CONTENT_RANGE.fullmatch(response.headers.get("Content-Range") or "")
            if (
                getattr(response, "status", 200) != 206
                or not match
                or not match.group(1)
                or match.group(3) == "*"
                or int(match.group(1)) != offset
            ):
                response.close()
                raise NotSegmentable("no usable byte ranges")
            total = int(match.group(3))
            if total > self.limit:
                response.close()
                raise common.TooLargeError(self.url)
            self.total = total
            etag = response.headers.get("ETag") or ""
            self.validator = (etag if etag and not etag.startswith("W/") else None) or response.headers.get(
                "Last-Modified"
            )
            return response, int(match.group(2))

    def _open(self, start: int, end: int):
        """A ranged request after the probe; returns (response, last byte it will send)."""
        response = self._request(start, end)
        match = common.CONTENT_RANGE.fullmatch(response.headers.get("Content-Range") or "")
        if getattr(response, "status", 200) != 206 or not match or not match.group(1):
            response.close()
            if self.validator:
                raise ConnectionError("The file changed on the server mid-download")
            raise ConnectionError("Server stopped honouring byte ranges mid-download")
        if int(match.group(1)) != start:
            response.close()
            raise ConnectionError("Server sent the wrong byte range")
        if match.group(3) != "*" and int(match.group(3)) != self.total:
            response.close()
            raise ConnectionError("The file changed on the server mid-download")
        return response, min(end, int(match.group(2)))

    def _retry_or_raise(self, error: BaseException, attempt: int) -> int:
        if self.stop.is_set() or attempt >= self.retries or not common.network_error(error):
            raise error
        attempt += 1
        with self.lock:
            kept = sum(s.done - s.start for s in self.segments)
            self.retry_events.append((error, attempt, kept))
        self.wake.set()
        _pause(self.stop, common.retry_delay(attempt))
        return attempt

    # ------------------------------------------------------------------ connections
    def _spawn(self, seg: Segment, primary: bool, response=None) -> None:
        with self.lock:
            wid = self._next_wid
            self._next_wid += 1
            conn = Conn(wid, primary, seg=seg)
            seg.owner = wid
            self.conns[wid] = conn
        thread = threading.Thread(
            target=self._worker, args=(conn, response), name=f"segment-{wid}", daemon=True
        )
        self.threads.append(thread)
        thread.start()

    def _worker(self, conn: Conn, response) -> None:
        chunker = Chunker(self.chunk_cap)
        try:
            while not self.stop.is_set():
                seg = conn.seg or self._claim(conn.wid)
                if seg is None:
                    return
                conn.seg = seg
                if not self._fill(conn, seg, chunker, response):
                    return  # refused: this connection is not wanted
                response = None
                with self.lock:
                    seg.owner = None
                    conn.seg = None
        except BaseException as error:
            with self.lock:
                if self.error is None and not self.stop.is_set():
                    self.error = error
                self.stop.set()
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response[0].close()
            with self.lock:
                if conn.seg is not None and conn.seg.owner == conn.wid:
                    conn.seg.owner = None
                self.conns.pop(conn.wid, None)
            self.wake.set()

    def _claim(self, wid: int) -> Segment | None:
        with self.lock:
            return self._claim_locked(wid)

    def _claim_locked(self, wid: int) -> Segment | None:
        """An unowned unfinished segment, or half of the largest one being downloaded."""
        free = [s for s in self.segments if s.owner is None and s.remaining > 0]
        if free:
            seg = min(free, key=lambda s: s.done)
            seg.owner = wid
            return seg
        return self._split_locked(wid)

    def _split_locked(self, wid: int, victim: Segment | None = None) -> Segment | None:
        if victim is None:
            busy = [s for s in self.segments if s.remaining > 0]
            victim = max(busy, key=lambda s: s.remaining, default=None)
        if victim is None or victim.remaining < 2 * self.min_split:
            return None
        mid = victim.done + victim.remaining // 2
        new = Segment(mid, victim.end, mid, owner=wid)
        victim.end = mid
        self.segments.append(new)
        self.segments.sort(key=lambda s: s.start)
        return new

    def _fill(self, conn: Conn, seg: Segment, chunker: Chunker, response) -> bool:
        """Download `seg` to its end. Returns False if the server refused this extra connection."""
        attempt = 0
        while not self.stop.is_set():
            with self.lock:
                start, end = seg.done, seg.end
            if start >= end:
                return True
            began = time.monotonic()
            try:
                if response is None:
                    response = self._open(start, min(start + chunker.size, end) - 1)
                r, last = response
                with r:
                    got = self._stream(conn, seg, r, start, last)
            except _Reconnect:
                response = None
                continue
            except Exception as error:
                response = None
                if not conn.primary and conn.bytes == 0 and status_of(error) in REFUSED:
                    with self.lock:
                        self.max_conns = max(1, len(self.conns) - 1)
                    log.info("server refused another connection (%s): limit %d", error, self.max_conns)
                    return False
                chunker.failed()
                attempt = self._retry_or_raise(error, attempt)
                continue
            response, attempt = None, 0
            chunker.measured(got, time.monotonic() - began)
        return True

    def _stream(self, conn: Conn, seg: Segment, r, start: int, last: int) -> int:
        pos, since_flush = start, 0
        writeback = common.Writeback(start)
        while True:
            if conn.reconnect:
                conn.reconnect = False
                raise _Reconnect
            data = r.read(common.CHUNK)
            if not data:
                break
            with self.lock:
                if self.stop.is_set():
                    return pos - start
                stop_at = min(seg.end, last + 1)
                n = min(len(data), stop_at - pos)
                if n > 0:
                    os.pwrite(self.fd, memoryview(data)[:n], pos)
                    pos += n
                    seg.done = pos
                    conn.bytes += n
                window = max(MIN_CHUNK, common.FLUSH_BYTES // max(1, len(self.conns)))
                fd = self.fd
            since_flush += max(n, 0)
            if since_flush >= window:
                writeback.step(fd, pos)
                since_flush = 0
            if pos >= stop_at:
                break
        with self.lock:
            stop_at = min(seg.end, last + 1)
        if pos < stop_at and not self.stop.is_set():
            raise common.ShortPart(f"Short part: {pos - start} of {stop_at - start} bytes")
        return pos - start

    # ------------------------------------------------------------------ coordinator
    def _coordinate(self) -> None:
        now = time.monotonic()
        ramp, rates = Ramp(now), deque([(now, self._done())])
        last_report, last_save = 0.0, now
        while True:
            self.wake.wait(TICK)
            self.wake.clear()
            now = time.monotonic()
            with self.lock:
                error, events, self.retry_events = self.error, self.retry_events, []
                alive = len(self.conns)
                incomplete = any(s.remaining > 0 for s in self.segments)
            if error is not None:
                self._abort()
                raise error
            done = self._done()
            for err, attempt, kept in events:
                self.on_retry(err, attempt, self.retries, common.retry_delay(attempt), kept)
            if not incomplete:
                return
            if alive == 0:  # every connection left (refusals): continue on a fresh one
                seg = self._claim(self._next_wid)
                if seg is None:
                    raise ConnectionError(f"Incomplete download: {done} of {self.total} bytes")
                with self.lock:
                    seg.owner = None
                self._spawn(seg, primary=True)
                continue
            try:
                if now - last_report >= REPORT_EVERY:
                    last_report = now
                    self.progress(done, self.total, connections=alive)
            except BaseException:
                self._abort()
                raise
            rates.append((now, done))
            while len(rates) > 2 and now - rates[0][0] > RAMP_EVERY:
                rates.popleft()
            span = rates[-1][0] - rates[0][0]
            rate = (rates[-1][1] - rates[0][1]) / span if span > 0 else 0.0
            with self.lock:
                max_conns = self.max_conns
            if ramp.should_add(now, rate, alive, max_conns):
                with self.lock:
                    seg = self._claim_locked(self._next_wid)
                if seg is not None:
                    self._spawn(seg, primary=False)
            self._replace_slow(now)
            if now - last_save >= SAVE_EVERY:
                last_save = now
                self._save_state()

    def _replace_slow(self, now: float) -> None:
        with self.lock:
            conns = list(self.conns.values())
            rates = {}
            for c in conns:
                c.marks.append((now, c.bytes))
                while len(c.marks) > 2 and now - c.marks[0][0] > SLOW_WINDOW:
                    c.marks.popleft()
                span = c.marks[-1][0] - c.marks[0][0]
                if span >= SLOW_WINDOW / 2:
                    rates[c.wid] = (c.marks[-1][1] - c.marks[0][1]) / span
            if len(rates) < 2:
                return
            median = statistics.median(rates.values())
            spawn = None
            for c in conns:
                rate = rates.get(c.wid)
                if rate is None or rate >= SLOW_RATIO * median:
                    c.slow_since = None
                    continue
                c.slow_since = c.slow_since or now
                if now - c.slow_since < SLOW_FOR or c.seg is None:
                    continue
                c.slow_since = None
                log.info("connection %d is slow (%.0f B/s, median %.0f): redistributing", c.wid, rate, median)
                if len(self.conns) < self.max_conns:
                    spawn = self._split_locked(self._next_wid, c.seg)
                else:
                    half = self._split_locked(-1, c.seg)
                    if half is not None:
                        half.owner = None  # the first connection to finish takes it
                    c.reconnect = True
                break
        if spawn is not None:
            self._spawn(spawn, primary=False)

    def _done(self) -> int:
        with self.lock:
            return sum(s.done - s.start for s in self.segments)

    def _abort(self) -> None:
        with self.lock:
            self.stop.set()
        for thread in self.threads:
            thread.join(JOIN_GRACE)
        self._save_state()
