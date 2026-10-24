"""严格 URL 白名单；逐跳检查重定向，不把任意 URL 交给上游。"""

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import getproxies

import httpx

from .domain import SourceError

HOSTS = {"xhslink.com", "xhslink.cn", "www.xiaohongshu.com", "xiaohongshu.com"}
SHORT_HOSTS = {"xhslink.com", "xhslink.cn"}
NOTE = re.compile(r"^/(?:explore|discovery/item)/([0-9a-f]{24})/?$")


PROFILE = re.compile(r"^/user/profile/([0-9a-f]{24})/?$")


def validate_url(url: str, *, allow_profiles=False) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or any(c.isspace() or ord(c) < 32 for c in url)
            or "\\" in url
        ):
            raise ValueError
        if parts.hostname not in SHORT_HOSTS and not (
            NOTE.fullmatch(parts.path)
            or allow_profiles
            and PROFILE.fullmatch(parts.path)
        ):
            raise ValueError
        if parts.hostname in SHORT_HOSTS and parts.path in ("", "/"):
            raise ValueError
        return urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))
    except ValueError:
        raise SourceError("INVALID_URL") from None


def note_id(url: str) -> str | None:
    parts = urlsplit(validate_url(url))
    match = NOTE.fullmatch(parts.path)
    return match.group(1) if parts.hostname not in SHORT_HOSTS and match else None


def extract_urls(text: str) -> list[str]:
    return re.findall(r'https?://[^\s<>"\[\]（）()，。；！？、]+', text)


def public_dns(host: str, allowed_hosts=None):
    if host not in (HOSTS if allowed_hosts is None else allowed_hosts):
        raise SourceError("SSRF_BLOCKED")
    # 平台HTTPS代理负责目标DNS；此模式下本机可能无法解析外网域名。
    # 仍逐跳限制为固定可信域名，保留平台出口策略与TLS校验，不改代理配置。
    if getproxies().get("https") or getproxies().get("all"):
        return
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise SourceError("DNS_ERROR", retryable=True) from None
    if not addresses or any(
        not ipaddress.ip_address(row[4][0]).is_global for row in addresses
    ):
        raise SourceError("SSRF_BLOCKED")


class Resolver:
    def __init__(self, client=None, check_dns=public_dns):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.check_dns = check_dns

    def resolve(self, url: str) -> str:
        for _ in range(6):
            url = validate_url(url, allow_profiles=True)
            if PROFILE.fullmatch(urlsplit(url).path) or note_id(url):
                return url
            self.check_dns(urlsplit(url).hostname)
            try:
                # 不读取正文，避免媒体下载和超大响应。
                with self.client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = response.headers.get("location")
                        if not target:
                            raise SourceError("INVALID_REDIRECT")
                        url = urljoin(url, target)
                        continue
                    if response.status_code in (403, 429):
                        raise SourceError("ACCESS_RESTRICTED", block=True)
                    raise SourceError("UNRESOLVED_LINK")
            except httpx.RequestError:
                raise SourceError("NETWORK_ERROR", retryable=True) from None
        raise SourceError("TOO_MANY_REDIRECTS")
