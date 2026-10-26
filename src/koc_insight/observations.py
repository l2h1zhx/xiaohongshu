"""品牌自然笔记：仅作观察，不进入投放、月报或后续自动采集。"""

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook

from .domain import SH
from .feishu import Feishu
from .project_import import import_plan
from .urls import Resolver, extract_urls

TABLE_NAME = "09 品牌自然观察"
SOURCE_LABEL = "vishin_wanxie_filtered_notes.xlsx"
FIELDS = {
    "笔记标题": 1,
    "Note ID": 1,
    "观察状态": 3,
    "关联说明": 1,
    "命中关键词": 1,
    "作者ID": 1,
    "作者昵称": 1,
    "发布时间": 5,
    "笔记类型": 1,
    "正文摘要": 1,
    "话题": 1,
    "点赞": 2,
    "收藏": 2,
    "评论": 2,
    "分享": 2,
    "数据口径": 1,
    "原笔记链接": 15,
    "来源文件": 1,
    "来源键": 1,
}


def _checked_workbook(path):
    path = Path(path)
    if path.suffix.lower() != ".xlsx" or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("需要不超过20MB的 .xlsx 观察文件")
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("观察文件展开超过100MB")
    return path


def _timestamp(value):
    instant = (
        value
        if isinstance(value, datetime)
        else datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    )
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=SH)
    return int(instant.timestamp() * 1000)


def _number(value):
    return None if value in (None, "") else int(value)


def _records(sheet, status):
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = {name: index for index, name in enumerate(rows[0]) if name}
    required = {"筛选结论", "笔记ID", "标题", "发布时间（北京时间）", "笔记链接URL"}
    if not required.issubset(headers):
        raise ValueError("观察文件缺少必要列")
    records = []

    def value(row, name):
        index = headers.get(name)
        return row[index] if index is not None else None

    for row in rows[1:]:
        note_id = row[headers["笔记ID"]]
        if not note_id:
            continue
        note_id = str(note_id)
        if len(note_id) != 24 or any(
            char not in "0123456789abcdef" for char in note_id
        ):
            raise ValueError("观察文件包含无效 Note ID")
        link = str(row[headers["笔记链接URL"]] or "")
        if not link.startswith("https://www.xiaohongshu.com/explore/"):
            raise ValueError("观察文件包含无效笔记链接")
        record = {
            "笔记标题": str(row[headers["标题"]] or "无标题"),
            "Note ID": note_id,
            "观察状态": status,
            "关联说明": str(row[headers["筛选结论"]] or ""),
            "命中关键词": str(value(row, "命中关键词") or ""),
            "作者昵称": str(value(row, "作者昵称") or ""),
            "发布时间": _timestamp(row[headers["发布时间（北京时间）"]]),
            "笔记类型": str(value(row, "笔记类型") or ""),
            "正文摘要": str(value(row, "正文/描述") or ""),
            "话题": str(value(row, "标签列表") or ""),
            "数据口径": "来源文件导入时的单次公开数据；不自动刷新，不纳入项目交付",
            "原笔记链接": {"text": "查看原笔记", "link": link},
            "来源文件": SOURCE_LABEL,
            "来源键": "natural:" + note_id,
        }
        for source, target in (
            ("点赞数", "点赞"),
            ("收藏数", "收藏"),
            ("评论数", "评论"),
            ("分享数", "分享"),
        ):
            number = _number(value(row, source))
            if number is not None:
                record[target] = number
        records.append(record)
    return records


def observation_plan(path):
    path = _checked_workbook(path)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if not {"相关笔记", "待复核"}.issubset(book.sheetnames):
            raise ValueError("观察文件需要包含“相关笔记”和“待复核”工作表")
        records = _records(book["相关笔记"], "相关") + _records(
            book["待复核"], "待复核"
        )
    finally:
        book.close()
    keys = [record["来源键"] for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("观察文件存在重复 Note ID")
    return {
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tables": [
            {
                "name": TABLE_NAME,
                "fields": FIELDS,
                "field_properties": {
                    "观察状态": {
                        "options": [
                            {"name": "相关", "color": 0},
                            {"name": "待复核", "color": 1},
                        ]
                    }
                },
                "hidden_fields": ["来源键"],
                "records": records,
            }
        ],
    }


def import_observations(service, path):
    """幂等导入；不调用小红书、不进入业务 notes 表。"""
    _ensure_observation_schema(service)
    plan = observation_plan(path)
    result = import_plan(service, plan)
    result["views"] = configure_observation_views(service)
    service.repo.set_meta(
        "natural_observations_import",
        str({"source": SOURCE_LABEL, "records": len(plan["tables"][0]["records"])}),
    )
    return {**result, "delivery_excluded": True, "one_time_capture": True}


def natural_link_plan(details, source_label="用户手工提交链接"):
    """把已采集的公开详情转换为自然观察行，不进入业务 notes 表。"""
    records = []
    for item in details:
        detail, link = item["detail"], item["link"]
        record = {
            "笔记标题": detail.title or "无标题",
            "Note ID": detail.note_id,
            "观察状态": "相关",
            "关联说明": "用户提交的品牌自然笔记；非项目投放，不纳入月报",
            "作者ID": detail.author_id or "",
            "作者昵称": detail.author_name or "",
            "笔记类型": detail.note_type or "",
            "正文摘要": detail.description or "",
            "话题": detail.tags or "",
            "数据口径": "录入时的单次公开采集；不自动刷新，不纳入项目交付",
            "原笔记链接": {"text": "查看原笔记", "link": link},
            "来源文件": source_label,
            "来源键": "natural:" + detail.note_id,
        }
        if detail.published_at:
            record["发布时间"] = int(
                datetime.fromisoformat(detail.published_at).timestamp() * 1000
            )
        for source, target in (
            ("likes", "点赞"),
            ("collects", "收藏"),
            ("comments", "评论"),
            ("shares", "分享"),
        ):
            value = detail.counts.get(source)
            if value is not None:
                record[target] = value
        records.append(record)
    keys = [record["来源键"] for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("提交链接解析为重复 Note ID")
    source = "\n".join(sorted(item["link"] for item in details)).encode()
    return {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "tables": [
            {
                "name": TABLE_NAME,
                "fields": FIELDS,
                "field_properties": {
                    "观察状态": {
                        "options": [
                            {"name": "相关", "color": 0},
                            {"name": "待复核", "color": 1},
                        ]
                    }
                },
                "hidden_fields": ["来源键"],
                "records": records,
            }
        ],
    }


def import_natural_links(service, text, resolver=None, collector=None, interval=5):
    """一次性采集用户给出的笔记链接；失败项不写入飞书。"""
    urls = list(dict.fromkeys(extract_urls(text)))
    if not urls:
        raise ValueError("没有找到小红书笔记链接")
    if len(urls) > 200:
        raise ValueError("一次最多处理200条自然观察链接")
    resolver = resolver or Resolver()
    owns_collector = collector is None
    if owns_collector:
        from .runtime import IsolatedCollector

        collector = IsolatedCollector()
    collected, failures = [], []
    try:
        for index, url in enumerate(urls):
            try:
                resolved = resolver.resolve(url)
                detail = collector.collect(resolved)
                collected.append({"detail": detail, "link": resolved})
            except Exception as error:  # 单条失败不可污染其余已采集结果。
                failures.append(
                    {"url": url, "error": getattr(error, "code", str(error))}
                )
            if index + 1 < len(urls) and interval:
                time.sleep(interval)
    finally:
        if owns_collector:
            collector.close()
    # 一次提交中的不同短链可能指向同一笔记；只保留首条，不重复写入。
    unique = {}
    for item in collected:
        unique.setdefault(item["detail"].note_id, item)
    plan = natural_link_plan(list(unique.values())) if unique else None
    if not plan:
        return {
            "submitted": len(urls),
            "captured": 0,
            "failed": failures,
            "delivery_excluded": True,
            "one_time_capture": True,
        }
    _ensure_observation_schema(service)
    result = import_plan(service, plan)
    result["views"] = configure_observation_views(service)
    result["accounts"] = sync_observation_accounts(service, list(unique.values()))
    return {
        **result,
        "submitted": len(urls),
        "captured": len(unique),
        "failed": failures,
        "delivery_excluded": True,
        "one_time_capture": True,
    }


def _ensure_observation_schema(service):
    """旧表创建于作者ID字段之前时，只补充该字段，不修改已有观察数据。"""
    manifest = json.loads(service.repo.meta("project_migration_tables", "{}"))
    table = manifest.get(TABLE_NAME)
    if not table:
        return
    client = Feishu(service.repo)
    path = f"/open-apis/bitable/v1/apps/{client.app}/tables/{table}"
    fields = {field["field_name"]: field for field in client.pages(path + "/fields")}
    if "作者ID" not in fields:
        client.request(
            "POST", path + "/fields", json={"field_name": "作者ID", "type": 1}
        )


def sync_observation_accounts(service, details):
    """自然笔记作者进入同一账号名册，但独立于投放篇数。"""
    manifest = json.loads(service.repo.meta("project_migration_tables", "{}"))
    table = manifest.get("03 账号与达人")
    if not table:
        raise ValueError("账号与达人表尚未建立")
    client = Feishu(service.repo)
    path = f"/open-apis/bitable/v1/apps/{client.app}/tables/{table}"
    fields = {field["field_name"]: field for field in client.pages(path + "/fields")}
    if "自然观察笔记数" not in fields:
        client.request(
            "POST", path + "/fields", json={"field_name": "自然观察笔记数", "type": 2}
        )
    rows = list(client.pages(path + "/records"))
    by_uid = {
        row.get("fields", {}).get("平台账号ID"): row
        for row in rows
        if row.get("fields", {}).get("平台账号ID")
    }
    counts, names = {}, {}
    for item in details:
        detail = item["detail"]
        if detail.author_id:
            counts[detail.author_id] = counts.get(detail.author_id, 0) + 1
            names.setdefault(detail.author_id, detail.author_name or detail.author_id)
    created, updates = 0, []
    existing_numbers = {str(row.get("fields", {}).get("编号", "")) for row in rows}
    sequence = 1
    for uid in sorted(counts):
        if uid in by_uid:
            row = by_uid[uid]
            if row.get("fields", {}).get("自然观察笔记数") != counts[uid]:
                updates.append(
                    {
                        "record_id": row["record_id"],
                        "fields": {"自然观察笔记数": counts[uid]},
                    }
                )
            continue
        while f"OBS-{sequence:03}" in existing_numbers:
            sequence += 1
        fields = {
            "编号": f"OBS-{sequence:03}",
            "账号/达人": names[uid],
            "平台账号ID": uid,
            "当前进度": "品牌自然观察；非投放，不纳入月报",
            "自然观察笔记数": counts[uid],
            "下一步": "仅作品牌自然内容观察，无需合作跟进",
            "主页链接": {
                "text": "查看作者主页",
                "link": "https://www.xiaohongshu.com/user/profile/" + uid,
            },
        }
        client.request("POST", path + "/records", json={"fields": fields})
        existing_numbers.add(fields["编号"])
        sequence += 1
        created += 1
    for start in range(0, len(updates), 100):
        client.request(
            "POST",
            path + "/records/batch_update",
            json={"records": updates[start : start + 100]},
        )
    return {"created": created, "updated": len(updates), "unique_accounts": len(counts)}


def configure_observation_views(service):
    """保留两个用途不同的视图，隐藏仅用于幂等导入的来源键。"""
    client = Feishu(service.repo)
    manifest = json.loads(service.repo.meta("project_migration_tables", "{}"))
    table = manifest.get(TABLE_NAME)
    if not table:
        raise ValueError("品牌自然观察表尚未建立")
    path = f"/open-apis/bitable/v1/apps/{client.app}/tables/{table}"
    fields = {field["field_name"]: field for field in client.pages(path + "/fields")}
    views = {
        view["view_name"]: view["view_id"] for view in client.pages(path + "/views")
    }
    if "工作台" in views and "全部观察" not in views:
        client.request(
            "PATCH", path + "/views/" + views["工作台"], json={"view_name": "全部观察"}
        )
        views["全部观察"] = views.pop("工作台")
    if "全部观察" not in views:
        views["全部观察"] = client.request(
            "POST", path + "/views", json={"view_name": "全部观察", "view_type": "grid"}
        )["view"]["view_id"]
    if "待复核" not in views:
        views["待复核"] = client.request(
            "POST", path + "/views", json={"view_name": "待复核", "view_type": "grid"}
        )["view"]["view_id"]
    hidden = [fields["来源键"]["field_id"]]
    client.request(
        "PATCH",
        path + "/views/" + views["全部观察"],
        json={"property": {"hidden_fields": hidden}},
    )
    status = fields["观察状态"]
    options = {item["name"]: item["id"] for item in status["property"]["options"]}
    condition = {
        "field_id": status["field_id"],
        "field_type": status["type"],
        "condition_id": "con"
        + hashlib.sha256(b"natural-observation-review").hexdigest()[:8],
        "operator": "is",
        "value": json.dumps([options["待复核"]], ensure_ascii=False),
    }
    client.request(
        "PATCH",
        path + "/views/" + views["待复核"],
        json={
            "property": {
                "hidden_fields": hidden,
                "filter_info": {"conjunction": "and", "conditions": [condition]},
            }
        },
    )
    return {"all": views["全部观察"], "review": views["待复核"]}
