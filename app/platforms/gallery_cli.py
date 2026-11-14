"""Run gallery-dl extraction with the same public-address policy as other fetches."""

import runpy

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from app.platforms.network import http_url, public_connection


class PublicHTTPConnection(HTTPConnection):
    def _new_conn(self):
        return public_connection(
            (self._dns_host, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        return public_connection(
            (self._dns_host, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": PublicHTTPPool, "https": PublicHTTPSPool}

    def send(self, request, **kwargs):
        http_url(request.url)
        if any((kwargs.get("proxies") or {}).values()):
            raise ValueError("Media extraction proxies bypass destination checks and are not supported.")
        return super().send(request, **kwargs)


class PublicSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.mount("http://", PublicAdapter())
        self.mount("https://", PublicAdapter())


def main():
    # Isolated CLI process: no global changes to Telegram or the PO-token provider.
    requests.Session = requests.sessions.Session = PublicSession
    runpy.run_module("gallery_dl", run_name="__main__")


if __name__ == "__main__":
    main()
