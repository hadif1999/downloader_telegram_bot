import io
import os
import urllib.error
from types import SimpleNamespace as NS

import pytest

from app.platforms import common, ytdlp

PAYLOAD = os.urandom(3500)


class Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None, stall_after=None):
        super().__init__(body)
        self.status, self.headers, self.stall_after = status, headers or {}, stall_after

    def read(self, n=-1):
        if self.stall_after is not None and self.tell() >= self.stall_after:
            raise TimeoutError("The read operation timed out")  # what a socket timeout raises
        return super().read(n if self.stall_after is None else min(n, self.stall_after - self.tell()))


class Server:
    """Fake CDN: serves PAYLOAD honouring Range unless told otherwise, logging every request.

    fail={start: n}: the next n requests starting at `start` drop the connection after 10 bytes.
    stall={start: n}: the same, but the connection goes silent (socket timeout) after 10 bytes.
    dead_from=x: every request starting at or after x drops before sending anything."""

    def __init__(
        self,
        ranges=True,
        fail=None,
        stall=None,
        stop_ranges_after=None,
        empty_range_416=False,
        dead_from=None,
    ):
        self.ranges, self.fail, self.stop_after = ranges, dict(fail or {}), stop_ranges_after
        self.stall, self.dead_from = dict(stall or {}), dead_from
        self.empty_416, self.requests, self.timeouts = empty_range_416, [], []

    def __call__(self, request, timeout=None):
        header = request.get_header("Range")
        self.requests.append(header)
        self.timeouts.append(timeout)
        if header and self.empty_416:
            raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)
        if not header or not self.ranges or (self.stop_after and len(self.requests) > self.stop_after):
            return Response(PAYLOAD, 200, {"Content-Length": str(len(PAYLOAD))})
        start, end = (int(x) for x in header.removeprefix("bytes=").split("-"))
        if start >= len(PAYLOAD):
            headers = {"Content-Range": f"bytes */{len(PAYLOAD)}"}
            raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", headers, None)
        end = min(end, len(PAYLOAD) - 1)
        headers = {"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"}
        if self.dead_from is not None and start >= self.dead_from:
            return Response(b"", 206, headers)
        if self.stall.get(start, 0) > 0:
            self.stall[start] -= 1
            return Response(PAYLOAD[start : end + 1], 206, headers, stall_after=10)
        if self.fail.get(start, 0) > 0:
            self.fail[start] -= 1
            body = PAYLOAD[start : start + 10]  # connection drops mid-part
        else:
            body = PAYLOAD[start : end + 1]
        return Response(body, 206, headers)


@pytest.fixture
def parts(monkeypatch):
    s = NS(download_part_bytes=1000, max_file_bytes=10_000, download_retries=3, download_timeout=7)
    monkeypatch.setattr(common, "get_settings", lambda: s)
    sleeps = []
    monkeypatch.setattr(common, "_sleep", sleeps.append)
    monkeypatch.setattr(common, "CHUNK", 500)  # smaller reads than a part, as with real files
    monkeypatch.setattr(common, "FLUSH_BYTES", 400)  # flush more often than a part, as in production
    released = []
    real = common.release_page_cache
    # Record how far the file was written each time its page cache was released.
    monkeypatch.setattr(
        common,
        "release_page_cache",
        lambda f: (released.append(f.tell() if hasattr(f, "tell") else f), real(f)),
    )

    def serve(server):
        monkeypatch.setattr(common, "open_url", server)
        return server

    return NS(settings=s, released=released, serve=serve, sleeps=sleeps)


def test_large_file_is_fetched_part_by_part_into_one_file(parts, tmp_path):
    server = parts.serve(Server())
    seen = []
    out = common.download_file(
        "https://cdn.test/v.mp4", tmp_path / "v.mp4", progress=lambda d, t, **part: seen.append((d, t, part))
    )
    assert out.read_bytes() == PAYLOAD
    assert server.requests == ["bytes=0-999", "bytes=1000-1999", "bytes=2000-2999", "bytes=3000-3499"]
    assert seen[-1][:2] == (3500, 3500) and [d for d, *_ in seen] == sorted(d for d, *_ in seen)
    # Each report names the part being fetched, so the status card can show it.
    assert seen[0][2] == {"part": 1, "parts": 4, "part_done": 500, "part_size": 1000}
    assert seen[-1][2] == {"part": 4, "parts": 4, "part_done": 500, "part_size": 500}
    # Dirty data never exceeds one flush interval (+ one read), independent of the 1000-byte parts.
    gaps = [b - a for a, b in zip([0, *parts.released], parts.released, strict=False)]
    assert max(gaps) <= 400 + 500 and parts.released[-1] == len(PAYLOAD)
    assert list(tmp_path.iterdir()) == [out]  # no separate part files to join


def test_failed_part_resumes_from_the_bytes_already_written(parts, tmp_path):
    server = parts.serve(Server(fail={2000: 1, 2010: 1}))
    retries = []
    out = common.download_file(
        "https://cdn.test/v.mp4",
        tmp_path / "v.mp4",
        progress=lambda d, t, **k: retries.append(k) if "retry" in k else None,
    )
    assert out.read_bytes() == PAYLOAD
    # Each retry asks only for the rest of the part: nothing received is fetched twice.
    assert server.requests == [
        "bytes=0-999",
        "bytes=1000-1999",
        "bytes=2000-2999",
        "bytes=2010-2999",
        "bytes=2020-2999",
        "bytes=3000-3499",
    ]
    assert [(r["retry"], r["retries"], r["kept"], r["part"]) for r in retries] == [
        (1, 3, 2010, 3),
        (2, 3, 2020, 3),
    ]
    assert retries[0]["reason"] == "the connection dropped mid-transfer"
    assert parts.sleeps == [2, 4]  # back-off between attempts


def test_stalled_part_times_out_and_resumes(parts, tmp_path):
    server = parts.serve(Server(stall={1000: 1}))
    reasons = []
    out = common.download_file(
        "https://cdn.test/v.mp4",
        tmp_path / "v.mp4",
        progress=lambda d, t, **k: reasons.append(k.get("reason")),
    )
    assert out.read_bytes() == PAYLOAD
    assert "bytes=1010-1999" in server.requests
    assert "the server stopped sending data" in reasons
    assert set(server.timeouts) == {7}  # DOWNLOAD_TIMEOUT is the per-request stall timeout


def test_part_failing_every_retry_raises(parts, tmp_path):
    server = parts.serve(Server(dead_from=1000))
    with pytest.raises(ConnectionError, match="Short part"):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert server.requests.count("bytes=1000-1999") == 4  # first try + DOWNLOAD_RETRIES
    assert (tmp_path / "v.mp4").stat().st_size == 1000  # finished parts stay for a later re-run


def test_retries_can_be_disabled(parts, tmp_path):
    parts.settings.download_retries = 0
    server = parts.serve(Server(fail={1000: 1}))
    with pytest.raises(ConnectionError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert server.requests == ["bytes=0-999", "bytes=1000-1999"]


def test_permanent_http_errors_are_not_retried(parts, tmp_path, monkeypatch):
    calls = []

    def gone(request, timeout=None):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(common, "open_url", gone)
    with pytest.raises(urllib.error.HTTPError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert len(calls) == 1


def test_rerun_resumes_a_partial_file_left_by_an_earlier_attempt(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(PAYLOAD[:1500])
    server = parts.serve(Server())
    seen = []
    out = common.download_file(
        "https://cdn.test/v.mp4", tmp_path / "v.mp4", progress=lambda d, t, **k: seen.append(d)
    )
    assert out.read_bytes() == PAYLOAD
    assert server.requests == ["bytes=1500-2499", "bytes=2500-3499"]
    assert seen[0] > 1500  # progress continues from the reused bytes


def test_rerun_of_a_complete_file_downloads_nothing(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(PAYLOAD)
    server = parts.serve(Server())
    assert common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4").read_bytes() == PAYLOAD
    assert server.requests == ["bytes=3500-4499"]


def test_rerun_against_a_server_without_ranges_starts_over(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"stale" * 100)
    parts.serve(Server(ranges=False))
    assert common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4").read_bytes() == PAYLOAD


def test_server_without_range_support_streams_once(parts, tmp_path):
    server = parts.serve(Server(ranges=False))
    out = common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert out.read_bytes() == PAYLOAD
    assert server.requests == ["bytes=0-999"]
    gaps = [b - a for a, b in zip([0, *parts.released], parts.released, strict=False)]
    assert max(gaps) <= 400 + 500  # still flushed periodically while streaming


def test_server_that_stops_honouring_ranges_is_an_error(parts, tmp_path):
    parts.serve(Server(stop_ranges_after=1))
    with pytest.raises(ConnectionError, match="byte ranges"):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")


def test_range_rejection_falls_back_to_plain_request(parts, tmp_path):
    server = parts.serve(Server(empty_range_416=True))
    out = common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert out.read_bytes() == PAYLOAD
    assert server.requests == ["bytes=0-999", None]


def test_oversized_total_is_rejected_from_first_part_header(parts, tmp_path):
    server = parts.serve(Server())
    with pytest.raises(common.TooLargeError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4", max_bytes=3000)
    assert server.requests == ["bytes=0-999"]


def test_small_file_needs_a_single_request(parts, tmp_path):
    parts.settings.download_part_bytes = 1 << 20
    server = parts.serve(Server())
    assert common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4").read_bytes() == PAYLOAD
    assert server.requests == ["bytes=0-1048575"]


def test_release_page_cache_keeps_file_contents(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(PAYLOAD)
    common.release_page_cache(path)
    with open(path, "rb+") as f:
        common.release_page_cache(f)
    assert path.read_bytes() == PAYLOAD
    common.release_page_cache(tmp_path / "missing")  # never raises


def test_ytdlp_progress_releases_page_cache_every_part(monkeypatch):
    released = []
    monkeypatch.setattr(ytdlp, "release_page_cache", released.append)
    progress = ytdlp.Progress(lambda *a, **k: None, None, flush_bytes=100)
    for done in (50, 99, 100, 150, 210):
        progress(
            {"status": "downloading", "tmpfilename": "v.part", "downloaded_bytes": done, "total_bytes": 300}
        )
    progress({"status": "finished", "filename": "v.mp4", "downloaded_bytes": 300})
    assert released == ["v.part", "v.part", "v.mp4"]


def test_ytdlp_downloads_in_http_range_chunks(monkeypatch, tmp_path):
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            path = tmp_path / "media.mp4"
            path.write_bytes(b"v")
            return {"requested_downloads": [{"filepath": str(path)}], "width": 1, "height": 1}

    monkeypatch.setattr(ytdlp, "YoutubeDL", FakeYDL)
    from app.models import FormatOption, MediaInfo

    info = MediaInfo("yt", "id", "https://youtu.be/x", "t", formats=[FormatOption("720", "720p", 1, 720)])
    ytdlp.YtDlpPlatform().download_url(info.url, info, "720", None, tmp_path, lambda *a: None)
    assert captured["http_chunk_size"] == 200 * 1024 * 1024
    # Retries and the stall timeout follow DOWNLOAD_RETRIES / DOWNLOAD_TIMEOUT (defaults 3 / 90 s).
    assert captured["retries"] == captured["fragment_retries"] == 3
    assert captured["socket_timeout"] == 90 and captured["continuedl"] is True
    assert callable(captured["retry_hook"]) and captured["retry_sleep_functions"]["http"](0) == 2
