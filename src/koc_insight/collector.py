import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .domain import METRICS, SH, Detail, SourceError, exact_count, stamp
from .urls import note_id, validate_url

UPSTREAM_SHA = "2ab2620b0dd35b04530e70f1e9c7ab3f78c5d6c5"
FIELD_MAP = {
    "likes": "点赞数量",
    "collects": "收藏数量",
    "comments": "评论数量",
    "shares": "分享数量",
}


def parse_upstream(payload: dict) -> Detail:
    """合约来自已检查的源码，尚不宣称为真实成功响应 Fixture。"""
    row = payload.get("data")
    if not isinstance(row, dict) or not row.get("作品ID"):
        # 上游会吞掉网络错误，空结果不能准确区分删除、风控或超时。
        raise SourceError("UPSTREAM_EMPTY_UNCLASSIFIED", block=True)
    counts, raw, available = {}, {}, {}
    for key in METRICS:
        value = row.get(FIELD_MAP.get(key, ""))
        # 当前已审查的公开详情合约没有曝光/阅读字段。
        raw[key] = None if value is None else str(value)
        counts[key], available[key] = exact_count(value)
    published = None
    timestamp = row.get("时间戳")
    if timestamp not in (None, "", 0):
        try:
            timestamp = float(timestamp)
            if timestamp > 1e11:
                timestamp /= 1000
            published = stamp(datetime.fromtimestamp(timestamp, UTC))
        except (ValueError, TypeError, OverflowError, OSError):
            raise SourceError("PARSE_ERROR") from None
    elif row.get("发布时间") not in (None, "", "未知"):
        # 上游格式化时间依赖服务器时区，必须显式知道该时区，不能猜成上海时间。
        raise SourceError("PUBLISH_TIMEZONE_UNKNOWN")
    return Detail(
        note_id=row["作品ID"],
        author_id=row.get("作者ID"),
        author_name=row.get("作者昵称"),
        title=row.get("作品标题"),
        description=row.get("作品描述"),
        note_type=row.get("作品类型"),
        tags=row.get("作品标签"),
        image_urls=row.get("下载地址", [])
        if row.get("作品类型") in ("图文", "图集")
        and isinstance(row.get("下载地址"), list)
        else [],
        image_count=len(row["下载地址"])
        if row.get("作品类型") in ("图文", "图集")
        and isinstance(row.get("下载地址"), list)
        else None,
        published_at=published,
        counts=counts,
        raw_counts=raw,
        availability=available,
        source_version=UPSTREAM_SHA,
    ).validate()


class XHSCollector:
    def __init__(self, client=None):
        self.base = os.getenv("XHS_API_BASE_URL", "http://127.0.0.1:5556").rstrip("/")
        parts = urlsplit(self.base)
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or parts.path
            or parts.username
            or parts.query
            or parts.fragment
        ):
            raise ValueError("XHS API 必须是本机 127.0.0.1 HTTP 服务")
        self.token = os.getenv("XHS_API_TOKEN", "")
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def collect(self, url: str) -> Detail:
        if os.getenv("XHS_READONLY_REVIEWED") != "true":
            raise SourceError("XHS_SECURITY_REVIEW_REQUIRED", block=True)
        if not self.token:
            raise SourceError("XHS_API_TOKEN_MISSING", block=True)
        validate_url(url)
        if not note_id(url):
            raise SourceError("RESOLVE_LINK_FIRST")
        try:
            response = self.client.post(
                self.base + "/xhs/detail",
                headers={"Authorization": "Bearer " + self.token},
                json={"url": url, "download": False, "check_record": False},
            )
            if response.status_code in (401, 403, 429):
                raise SourceError("ACCESS_RESTRICTED", block=True)
            if response.status_code >= 500:
                raise SourceError("UPSTREAM_5XX", retryable=True)
            if response.status_code != 200:
                raise SourceError("UPSTREAM_HTTP_ERROR")
            return parse_upstream(response.json())
        except httpx.RequestError:
            raise SourceError("NETWORK_ERROR", retryable=True) from None
        except (ValueError, TypeError, KeyError):
            raise SourceError("PARSE_ERROR") from None


class MockCollector:
    def __init__(self, published_at=None):
        self.published_at = published_at or stamp(datetime(2026, 10, 15, 10, tzinfo=SH))
        self.calls = 0

    def collect(self, url: str) -> Detail:
        self.calls += 1
        return Detail(
            note_id=note_id(url),
            author_id="mock-author",
            author_name="演示作者（非真实数据）",
            title="演示笔记（非真实数据）",
            description="Mock 数据，仅用于验证程序。",
            note_type="图文",
            tags="演示",
            published_at=self.published_at,
            counts={
                "likes": 10 * self.calls,
                "collects": 3,
                "comments": 2,
                "shares": None,
                "exposure": None,
                "reads": None,
            },
            source="mock",
            source_version="mock-v1",
        ).validate()
