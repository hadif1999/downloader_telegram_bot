"""Adaptive segmented downloads: parallel connections, work stealing, adaptive request sizes."""

import hashlib
import io
import json
import os
import threading
import time
import urllib.error
from types import SimpleNamespace as NS

import pytest
from yt_dlp import YoutubeDL as BaseYoutubeDL

from app.platforms import common, downloader, segmented
from app.platforms.segmented import Chunker, Conn, Ramp, Segment, SegmentedDownload
from app.services.progress import Cancelled, Monitor

PAYLOAD = os.urandom(20_000)


class Response(io.BytesIO):
    def __init__(self, body: bytes, status: int, headers: dict, delay: float = 0.0):
        super().__init__(body)
        self.status, self.headers, self.delay = status, headers, delay

    def read(self, n=-1):
        if self.delay:
            time.sleep(self.delay)  # a slow link: keeps connections busy so others can join
        return super().read(n)


class Server:
    """Thread-safe fake CDN serving PAYLOAD with Range, recording who asked for what.

    refuse_extra: extra connections (threads other than segment-0) get this HTTP status.
    etag: sent with every reply; change_etag_after=n: the file "changes" after n requests,
    so a request with a stale If-Range gets the whole new file (200) as HTTP requires."""

    def __init__(self, delay=0.0, refuse_extra=None, etag=None, change_etag_after=None, body=PAYLOAD):
        self.delay, self.refuse_extra, self.etag, self.change_after = (
            delay,
            refuse_extra,
            etag,
            change_etag_after,
        )
        self.body = body
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str | None, str | None]] = []  # (thread, Range, If-Range)

    def __call__(self, url, headers, timeout):
        thread = threading.current_thread().name
        header, if_range = headers.get("Range"), headers.get("If-Range")
        with self.lock:
            self.requests.append((thread, header, if_range))
            count = len(self.requests)
        etag = self.etag
        if self.change_after is not None and count > self.change_after:
            etag = '"v2"'
        if self.refuse_extra and thread not in ("segment-0", "MainThread"):
            raise urllib.error.HTTPError(url, self.refuse_extra, "Refused", {}, None)
        start, end = (int(x) for x in header.removeprefix("bytes=").split("-"))
        if if_range and etag and if_range != etag:
            return Response(self.body, 200, {"Content-Length": str(len(self.body)), "ETag": etag})
        end = min(end, len(self.body) - 1)
        reply = {"Content-Range": f"bytes {start}-{end}/{len(self.body)}"}
        if etag:
            reply["ETag"] = etag
        return Response(self.body[start : end + 1], 206, reply, self.delay)

    def threads(self) -> set[str]:
        return {t for t, _, _ in self.requests}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(segmented, "MIN_CHUNK", 256)
    monkeypatch.setattr(segmented, "FIRST_CHUNK", 1024)
    monkeypatch.setattr(segmented, "TICK", 0.002)
    monkeypatch.setattr(segmented, "REPORT_EVERY", 0)
    monkeypatch.setattr(segmented, "RAMP_EVERY", 0)
    monkeypatch.setattr(segmented, "MIN_GAIN", -1)  # always add a connection when allowed
    monkeypatch.setattr(segmented, "_pause", lambda event, seconds: None)
    monkeypatch.setattr(common, "CHUNK", 512)
    monkeypatch.setattr(common, "FLUSH_BYTES", 2048)


def engine(server, dest, **kw):
    options = {
        "headers": {"User-Agent": "test"},
        "timeout": 5,
        "limit": 10**9,
        "retries": 3,
        "max_conns": 4,
        "min_split": 1024,
        "chunk_cap": 2048,
    } | kw
    return SegmentedDownload("https://cdn.test/v.mp4", dest, server, **options)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------- parallel downloads


def test_large_file_is_downloaded_over_several_connections(tmp_path):
    server = Server(delay=0.002)
    seen = []
    dest = tmp_path / "v.mp4"
    assert engine(server, dest, progress=lambda d, t, **k: seen.append((d, t, k))).run() == len(PAYLOAD)
    assert digest(dest) == hashlib.sha256(PAYLOAD).hexdigest()
    assert len(server.threads()) > 1  # connections were added while the first one ran
    assert max(k.get("connections", 0) for *_, k in seen) <= 4
    assert seen[-1][:2] == (len(PAYLOAD), len(PAYLOAD))
    assert [d for d, *_ in seen] == sorted(d for d, *_ in seen)
    assert list(tmp_path.iterdir()) == [dest]  # the segment table is removed when complete


def test_connections_never_exceed_the_limit(tmp_path):
    server = Server(delay=0.002)
    engine(server, tmp_path / "v.mp4", max_conns=2).run()
    assert len(server.threads()) <= 2 + 1  # +1: a finished connection's successor may reuse a slot


def test_small_files_are_not_split(tmp_path):
    server = Server(delay=0.002)
    engine(server, tmp_path / "v.mp4", min_split=len(PAYLOAD)).run()
    assert server.threads() - {"MainThread"} == {"segment-0"}  # the probe runs on the caller


def test_a_refused_extra_connection_lowers_the_limit_instead_of_failing(tmp_path):
    server = Server(delay=0.001, refuse_extra=429)
    dest = tmp_path / "v.mp4"
    download = engine(server, dest)
    download.run()
    assert dest.read_bytes() == PAYLOAD
    assert download.max_conns == 1  # stopped asking for more after the server said no


def test_a_changed_file_is_detected_with_if_range(tmp_path):
    server = Server(etag='"v1"', change_etag_after=2)
    with pytest.raises(ConnectionError, match="changed on the server"):
        engine(server, tmp_path / "v.mp4", max_conns=1).run()
    assert server.requests[1][2] == '"v1"'  # later requests carry the probe's validator


def test_weak_etags_are_not_used_as_validators(tmp_path):
    server = Server(etag='W/"v1"')
    engine(server, tmp_path / "v.mp4", max_conns=1).run()
    assert all(if_range is None for *_, if_range in server.requests)


def test_cancel_from_progress_stops_every_connection(tmp_path):
    server = Server(delay=0.005)
    calls = []

    def progress(done, total, **k):
        calls.append(done)
        if len(calls) > 5:
            raise Cancelled

    with pytest.raises(Cancelled):
        engine(server, tmp_path / "v.mp4", progress=progress).run()
    time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("segment-")]
    assert (tmp_path / "v.mp4.segments.json").exists()  # a re-run resumes what was fetched


def test_rerun_after_cancel_fetches_only_the_missing_bytes(tmp_path):
    dest = tmp_path / "v.mp4"
    calls = []

    def cancel_soon(done, total, **k):
        calls.append(done)
        if len(calls) > 5:
            raise Cancelled

    with pytest.raises(Cancelled):
        engine(Server(delay=0.005), dest, progress=cancel_soon).run()
    table = json.loads((tmp_path / "v.mp4.segments.json").read_text())
    kept = sum(done - start for start, _, done in table["segments"])
    server = Server()
    engine(server, dest).run()
    assert dest.read_bytes() == PAYLOAD
    fetched = sum(
        int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for _, r, _ in server.requests
    )
    assert fetched <= len(PAYLOAD) - kept + len(server.requests) * 1024  # only gaps (+ request slack)


def test_segment_table_of_a_different_size_starts_over(tmp_path):
    dest = tmp_path / "v.mp4"
    dest.write_bytes(b"\0" * 5000)
    (tmp_path / "v.mp4.segments.json").write_text(json.dumps({"total": 5000, "segments": [[0, 5000, 2000]]}))
    engine(Server(), dest).run()
    assert dest.read_bytes() == PAYLOAD


def test_corrupt_segment_table_is_discarded(tmp_path):
    dest = tmp_path / "v.mp4"
    dest.write_bytes(b"x" * len(PAYLOAD))
    (tmp_path / "v.mp4.segments.json").write_text("{not json")
    engine(Server(), dest).run()
    assert dest.read_bytes() == PAYLOAD


def test_download_file_uses_parallel_connections(monkeypatch, tmp_path):
    settings = NS(
        max_file_bytes=10**9,
        download_retries=3,
        download_timeout=5,
        download_connections=4,
        download_min_split_bytes=1024,
        download_chunk_max_bytes=2048,
    )
    monkeypatch.setattr(common, "get_settings", lambda: settings)
    server = Server(delay=0.002)
    monkeypatch.setattr(
        common,
        "open_url",
        lambda request, timeout: server(request.full_url, dict(request.header_items()), timeout),
    )
    out = common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert out.read_bytes() == PAYLOAD and len(server.threads()) > 1


# --------------------------------------------------------------- the moving parts


def test_work_stealing_splits_the_largest_remaining_range():
    d = engine(Server(), "unused", min_split=100)
    d.segments = [Segment(0, 1000, 900, owner=0), Segment(1000, 3000, 1200, owner=1)]
    new = d._claim(2)
    assert (new.start, new.end, new.owner) == (2100, 3000, 2)
    assert d.segments[1].end == 2100  # the victim keeps the first half of what it had left
    assert d._claim(3).start == 1650  # the next steal halves the now-largest range again


def test_free_ranges_are_claimed_before_stealing():
    d = engine(Server(), "unused", min_split=100)
    d.segments = [Segment(0, 1000, 400, owner=0), Segment(1000, 2000, 1000, owner=None)]
    assert d._claim(1) is d.segments[1]


def test_ranges_smaller_than_two_splits_are_left_alone():
    d = engine(Server(), "unused", min_split=1000)
    d.segments = [Segment(0, 3000, 1100, owner=0)]
    assert d._claim(1) is None


def test_request_size_grows_while_the_speed_holds_and_shrinks_when_it_drops():
    c = Chunker(cap=8192)
    assert c.size == 1024
    c.measured(1024, 1.0)  # first sample only sets the reference
    c.measured(1024, 1.0)
    assert c.size == 2048
    c.measured(2048, 1.0)  # twice the rate
    assert c.size == 4096
    c.measured(4096, 100.0)  # collapsed: YouTube-style stall on a large range
    assert c.size == 2048
    c.failed()
    assert c.size == 1024 and c.rate is None
    for _ in range(10):
        c.measured(c.size, 0.001)
    assert c.size == 8192  # capped


def test_requests_cut_short_by_the_range_end_do_not_change_the_size():
    c = Chunker(cap=8192)
    c.measured(1024, 1.0)
    c.measured(10, 1.0)
    assert c.size == 1024


def test_youtube_requests_are_capped_at_10_mb(monkeypatch):
    monkeypatch.setattr(segmented, "MIN_CHUNK", 1 << 20)
    assert segmented.chunk_cap_for("https://rr1---sn-x.googlevideo.com/videoplayback", 64 << 20) == 10 << 20
    assert segmented.chunk_cap_for("https://cdn.example.com/v.mp4", 64 << 20) == 64 << 20


def test_ramp_adds_connections_while_they_raise_the_speed(monkeypatch):
    monkeypatch.setattr(segmented, "RAMP_EVERY", 1.0)
    monkeypatch.setattr(segmented, "MIN_GAIN", 0.1)
    ramp = Ramp(now=0)
    assert not ramp.should_add(0.5, 100, 1, 4)  # too soon
    assert ramp.should_add(1.0, 100, 1, 4)
    assert ramp.should_add(2.0, 180, 2, 4)  # +80 %: worth it
    assert not ramp.should_add(3.0, 185, 3, 4)  # +3 %: the server is the bottleneck now
    assert not ramp.should_add(9.0, 900, 3, 4)  # and it stays that way
    assert not Ramp(0).should_add(5.0, 100, 4, 4)  # never above the limit


def test_a_slow_connection_hands_half_of_its_range_over(monkeypatch):
    monkeypatch.setattr(segmented, "SLOW_FOR", 10.0)
    d = engine(Server(), "unused", min_split=100, max_conns=2)
    slow, fast = Segment(0, 10_000, 1000, owner=0), Segment(10_000, 20_000, 11_000, owner=1)
    d.segments = [slow, fast]
    d.conns = {0: Conn(0, True, seg=slow), 1: Conn(1, False, seg=fast)}
    for now in range(0, 30, 2):
        d.conns[0].bytes += 10  # 5 B/s
        d.conns[1].bytes += 1000  # 500 B/s
        d._replace_slow(float(now))
        if d.conns[0].reconnect:
            break
    assert d.conns[0].reconnect  # at the limit: reconnect, and leave half of the range free
    assert slow.end < 10_000 and any(s.owner is None and s.start == slow.end for s in d.segments)


def test_monitor_shows_the_connections():
    m = Monitor(now=lambda: 0.0)
    m(500, 1000, connections=3)
    assert "3 parallel connections" in m.render()
    m(600, 1000, connections=1)
    assert "parallel connections" not in m.render()


# --------------------------------------------------------------- yt-dlp


def test_ytdlp_uses_the_segmented_downloader_for_http_formats_of_known_size():
    pick = downloader._ydl_module.get_suitable_downloader
    fmt = {"url": "https://rr1.googlevideo.com/videoplayback", "protocol": "https", "filesize": 10}
    assert pick(dict(fmt), {"segmented_http": 4}) is downloader.SegmentedHttpFD
    assert pick(dict(fmt), {}) is not downloader.SegmentedHttpFD  # option off
    assert pick({**fmt, "filesize": None}, {"segmented_http": 4}) is not downloader.SegmentedHttpFD
    hls = {"url": "https://x.test/v.m3u8", "protocol": "m3u8_native", "filesize": 10}
    assert pick(hls, {"segmented_http": 4}) is not downloader.SegmentedHttpFD


def test_segmented_ytdlp_downloader_writes_the_file_and_reports_progress(monkeypatch, tmp_path):
    server = Server()
    ydl = BaseYoutubeDL({"quiet": True, "segmented_http": 4})
    monkeypatch.setattr(ydl, "urlopen", lambda request: server(request.url, dict(request.headers), 5))
    events = []
    fd = downloader.SegmentedHttpFD(ydl, ydl.params)
    fd.add_progress_hook(events.append)
    target = tmp_path / "media.f137.mp4"
    info = {"url": "https://rr1.googlevideo.com/videoplayback", "http_headers": {"User-Agent": "x"}}
    assert fd.real_download(str(target), info) is True
    assert target.read_bytes() == PAYLOAD and not (tmp_path / "media.f137.mp4.part").exists()
    assert events[0]["status"] == "downloading" and events[0]["segmented"] is True
    assert events[-1]["status"] == "finished" and events[-1]["total_bytes"] == len(PAYLOAD)
