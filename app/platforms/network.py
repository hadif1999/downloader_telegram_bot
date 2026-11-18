"""HTTP boundaries for untrusted platform pages and media URLs."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.request
from urllib.parse import urlsplit


def http_url(url: str):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("Only public HTTP(S) media URLs are supported.")
    return parsed


def on_domains(url: str, domains: tuple[str, ...]) -> bool:
    try:
        host = http_url(url).hostname.lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
    """Resolve once and connect to the checked address; keep the hostname for TLS SNI."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(
        not ipaddress.ip_address(a[4][0]).is_global or ipaddress.ip_address(a[4][0]).is_multicast
        for a in addresses
    ):
        raise OSError("Private or reserved media destinations are blocked.")
    last_error = None
    for family, kind, proto, _, sockaddr in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            for option in kwargs.get("socket_options") or ():
                sock.setsockopt(*option)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    raise last_error or OSError("No public destination found.")


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        http_url(req.full_url)
        return self.do_open(PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        http_url(req.full_url)
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(request: urllib.request.Request, timeout: float):
    http_url(request.full_url)
    # No environment proxy: it could bypass the checked connection destination.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), PublicHTTPHandler(), PublicHTTPSHandler(), PublicRedirectHandler()
    )
    return opener.open(request, timeout=timeout)
