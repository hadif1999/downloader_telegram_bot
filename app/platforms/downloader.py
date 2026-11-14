"""Keep yt-dlp cookie writes and untrusted HTTP inside operation-local boundaries."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL as BaseYoutubeDL
from yt_dlp.networking._urllib import HTTPHandler, UrllibRH

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
