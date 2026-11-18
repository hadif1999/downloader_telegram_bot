import io
import json
import os
import urllib.error
from types import SimpleNamespace as NS

import pytest

from app.platforms import common, segmented, ytdlp

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
    """download_file over one connection with a fixed 400-byte request size: deterministic requests.
    tests/test_segmented.py covers parallel connections and the adaptive request size."""
    s = NS(
        max_file_bytes=10_000,
        download_retries=3,
        download_timeout=7,
        download_connections=1,
        download_min_split_bytes=1000,
        download_chunk_max_bytes=1000,
    )
    monkeypatch.setattr(common, "get_settings", lambda: s)
    sleeps = []
    monkeypatch.setattr(common, "_sleep", sleeps.append)
    monkeypatch.setattr(segmented, "_pause", lambda event, seconds: sleeps.append(seconds))
    monkeypatch.setattr(segmented, "MIN_CHUNK", 100)
    monkeypatch.setattr(segmented, "FIRST_CHUNK", 400)
    monkeypatch.setattr(segmented, "TICK", 0.001)
    monkeypatch.setattr(segmented, "REPORT_EVERY", 0)
    monkeypatch.setattr(segmented.Chunker, "measured", lambda self, nbytes, seconds: None)
    monkeypatch.setattr(common, "CHUNK", 500)  # reads up to 500 bytes, as with real files
    monkeypatch.setattr(common, "FLUSH_BYTES", 400)  # flush often, as in production
    released = []
    step = common.Writeback.step
    # Record how far the file was written each time background writeback was started.
    monkeypatch.setattr(
        common.Writeback, "step", lambda self, fd, upto: (released.append(upto), step(self, fd, upto))
    )

    def serve(server):
        monkeypatch.setattr(common, "open_url", server)
        return server

    return NS(settings=s, released=released, serve=serve, sleeps=sleeps)


RANGES = [f"bytes={a}-{min(a + 400, 3500) - 1}" for a in range(0, 3500, 400)]


def test_large_file_is_fetched_range_by_range_into_one_file(parts, tmp_path):
    server = parts.serve(Server())
    seen = []
    out = common.download_file(
        "https://cdn.test/v.mp4", tmp_path / "v.mp4", progress=lambda d, t, **k: seen.append((d, t, k))
    )
    assert out.read_bytes() == PAYLOAD
    # The probe (first request) reveals the size and is also the first range of the download.
    assert server.requests == RANGES
    assert seen[-1][:2] == (3500, 3500) and [d for d, *_ in seen] == sorted(d for d, *_ in seen)
    assert all(t == 3500 for _, t, _ in seen) and "part" not in seen[0][2]
    # Dirty data never exceeds one flush window (+ one read).
    gaps = [b - a for a, b in zip([0, *parts.released], parts.released, strict=False)]
    assert gaps and max(gaps) <= 400 + 500
    assert list(tmp_path.iterdir()) == [out]  # no part files to join, no segment table left


def test_failed_request_resumes_from_the_bytes_already_written(parts, tmp_path):
    server = parts.serve(Server(fail={2000: 1, 2010: 1}))
    retries = []
    out = common.download_file(
        "https://cdn.test/v.mp4",
        tmp_path / "v.mp4",
        progress=lambda d, t, **k: retries.append(k) if "retry" in k else None,
    )
    assert out.read_bytes() == PAYLOAD
    # Each retry asks only for what is missing, with a smaller request: nothing is fetched twice.
    assert server.requests[5:8] == ["bytes=2000-2399", "bytes=2010-2209", "bytes=2020-2119"]
    assert [(r["retry"], r["retries"], r["kept"]) for r in retries] == [(1, 3, 2010), (2, 3, 2020)]
    assert retries[0]["reason"] == "the connection dropped mid-transfer"
    assert parts.sleeps == [2, 4]  # back-off between attempts


def test_stalled_request_times_out_and_resumes(parts, tmp_path):
    server = parts.serve(Server(stall={1200: 1}))
    reasons = []
    out = common.download_file(
        "https://cdn.test/v.mp4",
        tmp_path / "v.mp4",
        progress=lambda d, t, **k: reasons.append(k.get("reason")),
    )
    assert out.read_bytes() == PAYLOAD
    assert any(r.startswith("bytes=1210-") for r in server.requests)
    assert "the server stopped sending data" in reasons
    assert set(server.timeouts) == {7}  # DOWNLOAD_TIMEOUT is the per-request stall timeout


def test_request_failing_every_retry_raises_and_keeps_the_segment_table(parts, tmp_path):
    server = parts.serve(Server(dead_from=1200))
    with pytest.raises(ConnectionError, match="Short part"):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert sum(r.startswith("bytes=1200-") for r in server.requests) == 4  # first try + retries
    # The file is preallocated; the table records what is on disk for a later re-run.
    table = json.loads((tmp_path / "v.mp4.segments.json").read_text())
    assert table == {"total": 3500, "segments": [[0, 3500, 1200]]}


def test_retries_can_be_disabled(parts, tmp_path):
    parts.settings.download_retries = 0
    server = parts.serve(Server(fail={400: 1}))
    with pytest.raises(ConnectionError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert server.requests == ["bytes=0-399", "bytes=400-799"]


def test_permanent_http_errors_are_not_retried(parts, tmp_path, monkeypatch):
    calls = []

    def gone(request, timeout=None):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(common, "open_url", gone)
    with pytest.raises(urllib.error.HTTPError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert len(calls) == 1


def test_rerun_resumes_from_the_segment_table(parts, tmp_path):
    parts.serve(Server(dead_from=1200))
    with pytest.raises(ConnectionError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    server = parts.serve(Server())
    out = common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert out.read_bytes() == PAYLOAD
    assert server.requests[0] == "bytes=1200-1599"  # nothing before 1200 is fetched again
    assert not (tmp_path / "v.mp4.segments.json").exists()


def test_rerun_resumes_a_partial_file_of_a_single_stream_download(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(PAYLOAD[:1500])
    server = parts.serve(Server())
    seen = []
    out = common.download_file(
        "https://cdn.test/v.mp4", tmp_path / "v.mp4", progress=lambda d, t, **k: seen.append(d)
    )
    assert out.read_bytes() == PAYLOAD
    assert server.requests[0] == "bytes=1500-1899"
    assert all(int(r.split("=")[1].split("-")[0]) >= 1500 for r in server.requests)
    assert seen[0] >= 1500  # progress continues from the reused bytes


def test_rerun_of_a_complete_file_downloads_nothing(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(PAYLOAD)
    server = parts.serve(Server())
    assert common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4").read_bytes() == PAYLOAD
    assert server.requests == ["bytes=3500-3899"]


def test_rerun_against_a_server_without_ranges_starts_over(parts, tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"stale" * 100)
    parts.serve(Server(ranges=False))
    assert common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4").read_bytes() == PAYLOAD


def test_server_without_range_support_streams_once(parts, tmp_path):
    server = parts.serve(Server(ranges=False))
    out = common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4")
    assert out.read_bytes() == PAYLOAD
    # The probe's reply shows there are no ranges; one plain stream then fetches the file.
    assert server.requests == ["bytes=0-399", "bytes=0-999"]
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
    assert server.requests == ["bytes=0-399", "bytes=0-999", None]


def test_oversized_total_is_rejected_from_the_probe(parts, tmp_path):
    server = parts.serve(Server())
    with pytest.raises(common.TooLargeError):
        common.download_file("https://cdn.test/v.mp4", tmp_path / "v.mp4", max_bytes=3000)
    assert server.requests == ["bytes=0-399"]


def test_small_file_needs_a_single_request(parts, tmp_path, monkeypatch):
    monkeypatch.setattr(segmented, "FIRST_CHUNK", 1 << 20)
    parts.settings.download_connections = 4
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
    monkeypatch.setattr(ytdlp.Writeback, "step_path", lambda self, path, upto: released.append(path))
    progress = ytdlp.Progress(lambda *a, **k: None, None, flush_bytes=100)
    for done in (50, 99, 100, 150, 210):
        progress(
            {"status": "downloading", "tmpfilename": "v.part", "downloaded_bytes": done, "total_bytes": 300}
        )
    progress({"status": "finished", "filename": "v.mp4", "downloaded_bytes": 300})
    assert released == ["v.part", "v.part", "v.mp4"]


def test_ytdlp_download_options(monkeypatch, tmp_path):
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
    # HTTP formats go through the segmented downloader (DOWNLOAD_CONNECTIONS); yt-dlp's own
    # downloader, used otherwise, keeps small ranges: YouTube nearly stalls large range requests.
    assert captured["segmented_http"] == captured["concurrent_fragment_downloads"] == 4
    assert captured["http_chunk_size"] == 10 * 1024 * 1024
    assert captured["js_runtimes"] == {}  # challenge solving is off unless YOUTUBE_JS_RUNTIMES is set
    # Retries and the stall timeout follow DOWNLOAD_RETRIES / DOWNLOAD_TIMEOUT (defaults 3 / 90 s).
    assert captured["retries"] == captured["fragment_retries"] == 3
    assert captured["socket_timeout"] == 90 and captured["continuedl"] is True
    assert callable(captured["retry_hook"]) and captured["retry_sleep_functions"]["http"](0) == 2


def test_writeback_keeps_at_most_two_windows_cached(tmp_path):
    path = tmp_path / "w.bin"
    wb = common.Writeback()
    with open(path, "wb") as f:
        for upto in (100, 250, 400):
            f.write(PAYLOAD[upto - (100 if upto == 100 else 150) : upto])
            f.flush()
            wb.step(f.fileno(), upto)
            # Everything below the previous step is on disk and evicted; the newest window is
            # being written in the background. The writer never waited for a full flush.
            assert wb.started == upto and wb.dropped == {100: 0, 250: 100, 400: 250}[upto]
        wb.step(f.fileno(), 300)  # rewinds (retries) never move the windows back
        assert (wb.dropped, wb.started) == (250, 400)
    assert path.read_bytes() == PAYLOAD[:400]
