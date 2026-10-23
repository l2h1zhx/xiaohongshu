import socket

import pytest

from koc_insight.domain import SourceError
from koc_insight.urls import public_dns


def test_platform_proxy_dns_does_not_require_local_dns(monkeypatch):
    monkeypatch.setattr(
        "koc_insight.urls.getproxies",
        lambda: {"https": "http://test-proxy.invalid:8080"},
    )

    def unexpected_dns(*args, **kwargs):
        raise AssertionError("代理模式不应本机解析外网域名")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    public_dns("xhslink.com")
    with pytest.raises(SourceError, match="SSRF_BLOCKED"):
        public_dns("attacker.invalid")


def test_direct_connection_rejects_private_dns(monkeypatch):
    monkeypatch.setattr("koc_insight.urls.getproxies", lambda: {})
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(SourceError, match="SSRF_BLOCKED"):
        public_dns("xhslink.com")
