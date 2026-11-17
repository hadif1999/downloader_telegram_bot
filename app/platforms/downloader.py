"""Keep yt-dlp cookie writes and untrusted HTTP inside operation-local boundaries."""

from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

from yt_dlp import YoutubeDL as BaseYoutubeDL
from yt_dlp.downloader.common import FileDownloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking import Request
from yt_dlp.networking._urllib import HTTPHandler, UrllibRH
from yt_dlp.utils import NO_DEFAULT

from app.config import get_settings
from app.platforms.network import PublicHTTPConnection, PublicHTTPSConnection, http_url


@contextlib.contextmanager
def writable_cookies(source: str | Path | None):
    if not source:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="media-cookies-") as directory:
        copy = Path(directory) / "cookies.txt"
        shutil.copyfile(source, copy)
        copy.chmod(0o600)
        yield copy


class PublicUrllibRH(UrllibRH):
    _SUPPORTED_URL_SCHEMES = ("http", "https", "data")

    def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
        opener = super()._create_instance({}, cookiejar, legacy_ssl_support)

        def connection_class(base, request):
            provider = get_settings().pot_provider_url.rstrip("/")
            # These two endpoints are trusted operator configuration, not media URLs.
            # The bgutil plugin uses the same yt-dlp director for its local sidecar.
            if provider and (request.full_url, request.get_method()) in {
                (provider + "/ping", "GET"),
                (provider + "/get_pot", "POST"),
            }:
                return base
            http_url(request.full_url)
            return PublicHTTPSConnection if base.__name__ == "HTTPSConnection" else PublicHTTPConnection

        for handler in opener.handlers:
            if isinstance(handler, HTTPHandler):
                handler._make_conn_class = connection_class
        return opener


class YoutubeDL(BaseYoutubeDL):
    """One private cookie copy per instance; never modify mounted source cookies."""

    def __init__(self, params=None, *args, **kwargs):
        options = dict(params or {})
        self._cookie_copy = writable_cookies(options.get("cookiefile"))
        copy = self._cookie_copy.__enter__()
        if copy:
            options["cookiefile"] = str(copy)
        options["proxy"] = ""
        try:
            super().__init__(options, *args, **kwargs)
        except BaseException:
            self._cookie_copy.__exit__(None, None, None)
            raise

    def build_request_director(self, handlers, preferences=None):
        return super().build_request_director([PublicUrllibRH])

    def close(self):
        try:
            super().close()
        finally:
            self._cookie_copy.__exit__(None, None, None)


_report_retry = FileDownloader.report_retry


def _report_retry_to_hook(self, err, count, retries, *args, **kwargs):
    """yt-dlp has progress hooks but no retry hook: forward its retries to the `retry_hook`
    option (a callable(error, count, retries)) so the status card can show them."""
    hook = self.ydl.params.get("retry_hook")
    if hook and 0 < count <= retries:
        hook(err, count, retries)
    return _report_retry(self, err, count, retries, *args, **kwargs)


FileDownloader.report_retry = _report_retry_to_hook


class SegmentedHttpFD(FileDownloader):
    """Plain HTTP(S) formats through the adaptive segmented downloader (`app.platforms.segmented`):
    parallel connections and request sizes that follow the speed, instead of yt-dlp's single
    connection in fixed `http_chunk_size` ranges. Requests go through yt-dlp's own request
    director, so cookies, signed URLs and the guarded public connections all still apply.
    Servers without byte ranges fall back to yt-dlp's HttpFD, resuming what is on disk."""

    FD_NAME = "segmented"

    def real_download(self, filename, info_dict):
        from app.platforms.segmented import NotSegmentable, SegmentedDownload

        settings = get_settings()
        tmp = self.temp_name(filename)
        started = time.monotonic()
        chunk_cap = settings.download_chunk_max_bytes
        if size := (info_dict.get("downloader_options") or {}).get("http_chunk_size"):
            chunk_cap = min(chunk_cap, size)
        retry_hook = self.ydl.params.get("retry_hook")

        def opener(url, headers, timeout):
            return self.ydl.urlopen(Request(url, headers=headers, extensions={"timeout": timeout}))

        first = [None]  # bytes already on disk when this run started (a resume)

        def progress(done, total, connections=0, **_):
            if first[0] is None:
                first[0] = done
            elapsed = time.monotonic() - started
            speed = self.calc_speed(started, time.monotonic(), done - first[0])
            self._hook_progress(
                {
                    "status": "downloading",
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "filename": filename,
                    "tmpfilename": tmp,
                    "elapsed": elapsed,
                    "speed": speed,
                    "eta": self.calc_eta(speed, total - done) if speed and total else None,
                    "segmented": True,
                    "connections": connections,
                },
                info_dict,
            )

        def on_retry(error, attempt, retries, wait, kept):
            if retry_hook:
                retry_hook(error, attempt, retries)

        try:
            total = SegmentedDownload(
                info_dict["url"],
                Path(tmp),
                opener,
                headers=dict(info_dict.get("http_headers") or {}),
                timeout=self.params.get("socket_timeout") or settings.download_timeout,
                limit=self.params.get("max_filesize") or settings.max_file_bytes,
                retries=settings.download_retries,
                max_conns=int(self.params.get("segmented_http") or 1),
                min_split=settings.download_min_split_bytes,
                chunk_cap=chunk_cap,
                progress=progress,
                on_retry=on_retry,
            ).run()
        except NotSegmentable:
            fallback = HttpFD(self.ydl, self.params)
            for hook in self._progress_hooks:
                fallback.add_progress_hook(hook)
            return fallback.real_download(filename, info_dict)
        self.try_rename(tmp, filename)
        self._hook_progress(
            {
                "status": "finished",
                "downloaded_bytes": total,
                "total_bytes": total,
                "filename": filename,
                "elapsed": time.monotonic() - started,
            },
            info_dict,
        )
        return True


_ydl_module = sys.modules[BaseYoutubeDL.__module__]
_get_suitable_downloader = _ydl_module.get_suitable_downloader


def _suitable_downloader(info_dict, params={}, default=NO_DEFAULT, protocol=None, to_stdout=False):  # noqa: B006
    """yt-dlp picks its downloader per format: use SegmentedHttpFD where it would use HttpFD for a
    format of known size, when the `segmented_http` option (max connections) is set."""
    fd = _get_suitable_downloader(info_dict, params, default, protocol, to_stdout)
    if (
        fd is HttpFD
        and params.get("segmented_http")
        and not to_stdout
        and not params.get("test")
        and not info_dict.get("is_live")
        and (info_dict.get("filesize") or info_dict.get("filesize_approx"))
    ):
        return SegmentedHttpFD
    return fd


_ydl_module.get_suitable_downloader = _suitable_downloader
