import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
METRICS = ("likes", "collects", "comments", "shares", "exposure", "reads")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


def stamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value))


def month_due(month: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("月份必须为 YYYY-MM")
    year, num = map(int, month.split("-"))
    datetime(year, num, 1)
    return datetime(year + (num == 12), num % 12 + 1, 1, 9, tzinfo=SH).astimezone(UTC)


def exact_count(value) -> tuple[int | None, str]:
    if value is None or isinstance(value, bool):
        return None, "missing"
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text), "exact"
    if re.fullmatch(r"\d{1,3}(,\d{3})+", text):
        return int(text.replace(",", "")), "exact"
    if re.fullmatch(r"\d+(?:\.\d+)?(?:万|亿|[kKwW])?\+?", text):
        return None, "approximate"
    return None, "missing" if text in ("", "-1", "null", "未知") else "invalid"


@dataclass
class Detail:
    note_id: str
    author_id: str | None = None
    author_name: str | None = None
    title: str | None = None
    description: str | None = None
    note_type: str | None = None
    tags: str | None = None
    published_at: str | None = None
    counts: dict = field(default_factory=dict)
    raw_counts: dict = field(default_factory=dict)
    availability: dict = field(default_factory=dict)
    source: str = "xhs-downloader"
    source_version: str = "unverified"
    evidence: str | None = None
    image_count: int | None = None

    def validate(self):
        if not re.fullmatch(r"[0-9a-f]{24}", self.note_id):
            raise ValueError("INVALID_NOTE_ID")
        if self.published_at:
            self.published_at = stamp(parse_time(self.published_at))
        if self.image_count is not None and (
            type(self.image_count) is not int or self.image_count < 0
        ):
            raise ValueError("INVALID_IMAGE_COUNT")
        for metric in METRICS:
            count = self.counts.get(metric)
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("INVALID_METRIC")
        if self.source == "manual" and not self.evidence:
            raise ValueError("人工数据需要说明证据来源和统计时间")
        return self

    def dict(self):
        return asdict(self)


class SourceError(Exception):
    def __init__(self, code: str, retryable=False, block=False):
        super().__init__(code)
        self.code, self.retryable, self.block = code, retryable, block
