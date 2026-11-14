import io
import socket
from pathlib import Path
from unittest.mock import MagicMock
from urllib.request import Request

import pytest

from app.platforms import network, registry
from app.platforms.downloader import PublicUrllibRH, YoutubeDL, writable_cookies
from app.platforms.gallery_cli import PublicSession


@pytest.mark.parametrize(
    "domain,path",
    [
        ("youtube.com", "watch?v=abcdefghijk"),
        ("instagram.com", "p/test/"),
        ("tiktok.com", "@name/video/123"),
        ("pinterest.com", "pin/123/"),
        ("snapchat.com", "spotlight/123"),
        ("likee.video", "@name/video/123"),
        ("vk.com", "video-1_2"),
        ("facebook.com", "watch/?v=123"),
        ("threads.net", "@name/post/code"),
        ("soundcloud.com", "name/track"),
        ("open.spotify.com", "track/code"),
    ],
)
def test_matchers_reject_domain_text_outside_hostname(domain, path):
    assert registry.detect(f"http://127.0.0.1/{domain}/{path}") is None
    assert registry.detect(f"https://unrelated.invalid/?next=https://{domain}/{path}") is None
    assert registry.detect(f"https://not{domain}/{path}") is None
    assert registry.detect(f"https://{domain}.unrelated.invalid/{path}") is None
    assert registry.detect(f"https://{domain}@127.0.0.1/{path}") is None


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_public_connection_rejects_nonpublic_dns_before_connect(monkeypatch, ip):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )
    sock = MagicMock()
    monkeypatch.setattr(network.socket, "socket", sock)
    with pytest.raises(OSError):
        network.public_connection(("media.example", 443), 1)
    sock.assert_not_called()


def test_public_connection_connects_checked_address_without_second_dns(monkeypatch):
    dns = MagicMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    sock = MagicMock()
    monkeypatch.setattr(network.socket, "getaddrinfo", dns)
    monkeypatch.setattr(network.socket, "socket", lambda *a: sock)
    assert network.public_connection(("media.example", 443), 2) is sock
    dns.assert_called_once()
    sock.connect.assert_called_once_with(("93.184.216.34", 443))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "https://a:b@example.com/",
        "https://example.com:8080/a",
        "https://example.com/\nfoo",
    ],
)
def test_http_url_rejects_unsupported_destinations(url):
    with pytest.raises(ValueError):
        network.http_url(url)


def test_redirect_to_file_is_rejected():
    handler = network.PublicRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(
            Request("https://example.com/"), io.BytesIO(), 302, "Found", {}, "file:///etc/passwd"
        )


def test_ytdlp_transport_uses_checked_connections_on_redirects():
    with YoutubeDL({"quiet": True}) as ydl:
        director = ydl._request_director
        assert len(director.handlers) == 1
        handler = next(iter(director.handlers.values()))
        assert isinstance(handler, PublicUrllibRH)
        opener = handler._create_instance({}, ydl.cookiejar)
        from yt_dlp.networking._urllib import HTTPHandler

        transport = next(h for h in opener.handlers if isinstance(h, HTTPHandler))
        import http.client

        assert (
            transport._make_conn_class(http.client.HTTPSConnection, Request("https://example.com/"))
            is network.PublicHTTPSConnection
        )
        with pytest.raises(ValueError):
            transport._make_conn_class(http.client.HTTPConnection, Request("file:///tmp/private"))


def test_cookie_source_stays_unchanged_and_copy_is_deleted(tmp_path):
    source = tmp_path / "cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    source.chmod(0o400)
    with YoutubeDL({"quiet": True, "cookiefile": str(source)}) as ydl:
        copy = Path(ydl.params["cookiefile"])
        assert copy != source and copy.is_file()
        assert copy.stat().st_mode & 0o777 == 0o600
    assert not copy.exists()
    assert source.read_text() == "# Netscape HTTP Cookie File\n"


def test_cookie_copy_cleanup_on_error(tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n")
    with pytest.raises(RuntimeError), writable_cookies(p) as copy:
        path = copy
        raise RuntimeError("extractor failed")
    assert not path.exists()


def test_gallery_session_uses_public_transport():
    with PublicSession() as session:
        assert session.trust_env is False
        pool = session.get_adapter("https://example.com").poolmanager.connection_from_url(
            "https://example.com"
        )
        from app.platforms.gallery_cli import PublicHTTPSConnection

        assert pool.ConnectionCls is PublicHTTPSConnection


def test_trusted_pot_provider_keeps_only_its_two_configured_endpoints(monkeypatch):
    import http.client
    from types import SimpleNamespace

    from yt_dlp.networking._urllib import HTTPHandler

    import app.platforms.downloader as downloader

    monkeypatch.setattr(
        downloader, "get_settings", lambda: SimpleNamespace(pot_provider_url="http://bgutil-provider:4416")
    )
    with YoutubeDL({"quiet": True}) as ydl:
        handler = next(iter(ydl._request_director.handlers.values()))
        opener = handler._create_instance({}, ydl.cookiejar)
        transport = next(h for h in opener.handlers if isinstance(h, HTTPHandler))
        pick = transport._make_conn_class
        assert (
            pick(http.client.HTTPConnection, Request("http://bgutil-provider:4416/ping"))
            is http.client.HTTPConnection
        )
        assert (
            pick(http.client.HTTPConnection, Request("http://bgutil-provider:4416/get_pot", data=b"{}"))
            is http.client.HTTPConnection
        )
        for request in (
            Request("http://bgutil-provider:4416/other"),
            Request("http://bgutil-provider:4416/get_pot"),
            Request("http://127.0.0.1:4416/ping"),
        ):
            with pytest.raises(ValueError):
                pick(http.client.HTTPConnection, request)
