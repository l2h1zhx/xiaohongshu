"""经检查的项目迁移计划：按来源键幂等追加，不覆盖人工维护的行。"""

import json
import re
from uuid import uuid4

from .domain import SourceError
from .feishu import Feishu


def validate_plan(plan):
    if not re.fullmatch(r"[0-9a-f]{64}", plan.get("source_sha256", "")):
        raise ValueError("缺少原文件指纹")
    if not 1 <= len(plan.get("tables", [])) <= 20:
        raise ValueError("迁移表数量无效")
    names = set()
    for table in plan["tables"]:
        name, fields = table["name"], table["fields"]
        if name in names or not name or len(name) > 80:
            raise ValueError("目标表名冲突")
        names.add(name)
        if fields.get("来源键") != 1 or not 1 <= len(fields) <= 100:
            raise ValueError("需要文本来源键")
        if any(t not in (1, 2, 15) for t in fields.values()):
            raise ValueError("不支持的迁移字段类型")
        keys = set()
        for row in table["records"]:
            key = row.get("来源键")
            if not key or key in keys or set(row) - set(fields):
                raise ValueError("来源键重复或出现未声明字段")
            keys.add(key)
            for label, value in row.items():
                kind = fields[label]
                if kind == 1 and (not isinstance(value, str) or len(value) > 20000):
                    raise ValueError("文本字段无效或过长")
                if kind == 2 and (type(value) not in (int, float)):
                    raise ValueError("数值字段无效")
                if kind == 15 and (
                    not isinstance(value, dict)
                    or not str(value.get("link", "")).startswith("https://")
                ):
                    raise ValueError("超链接字段无效")


def import_plan(service, plan):
    validate_plan(plan)
    client, repo = Feishu(service.repo), service.repo
    base = f"/open-apis/bitable/v1/apps/{client.app}/tables"
    manifest = json.loads(repo.meta("project_migration_tables", "{}"))
    results = []
    for target in plan["tables"]:
        name = target["name"]
        table = manifest.get(name)
        creation_key = "project_migration_create:" + name
        if not table:
            matches = [t for t in client.pages(base) if t.get("name") == name]
            state = repo.meta(creation_key)
            if matches and not state:
                raise SourceError("PROJECT_TABLE_NAME_CONFLICT")
            if len(matches) > 1:
                raise SourceError("PROJECT_TABLE_NAME_CONFLICT")
            if matches:
                table = matches[0]["table_id"]
            else:
                if state == "UNCERTAIN":
                    raise SourceError("PROJECT_TABLE_CREATE_UNCERTAIN")
                repo.set_meta(creation_key, "CREATING")
                _checkpoint(service)
                try:
                    data = client.request(
                        "POST",
                        base,
                        json={
                            "table": {
                                "name": name,
                                "default_view_name": "工作台",
                                "fields": [
                                    {"field_name": f, "type": t}
                                    for f, t in target["fields"].items()
                                ],
                            }
                        },
                    )
                    table = data["table_id"]
                except SourceError:
                    repo.set_meta(creation_key, "UNCERTAIN")
                    raise
            manifest[name] = table
            repo.set_meta(
                "project_migration_tables", json.dumps(manifest, ensure_ascii=False)
            )
            repo.set_meta(creation_key, "DONE")
            _checkpoint(service)
        path = base + "/" + table
        schema = {f["field_name"]: f for f in client.pages(path + "/fields")}
        if any(
            label not in schema or schema[label]["type"] != kind
            for label, kind in target["fields"].items()
        ):
            raise SourceError("PROJECT_IMPORT_SCHEMA_CHANGED")
        existing = {}
        for record in client.pages(path + "/records"):
            key = record.get("fields", {}).get("来源键")
            if isinstance(key, list):
                key = "".join(v.get("text", "") for v in key)
            if key:
                if key in existing:
                    raise SourceError("PROJECT_IMPORT_DUPLICATE_SOURCE")
                existing[key] = record["record_id"]
        pending = [r for r in target["records"] if r["来源键"] not in existing]
        for start in range(0, len(pending), 100):
            batch = pending[start : start + 100]
            token_key = "project_migration_batch:" + name + ":" + batch[0]["来源键"]
            token = repo.meta(token_key) or str(uuid4())
            repo.set_meta(token_key, token)
            _checkpoint(service)
            data = client.request(
                "POST",
                path + "/records/batch_create",
                params={"client_token": token},
                json={"records": [{"fields": r} for r in batch]},
            )
            records = data.get("records", [])
            if len(records) != len(batch):
                raise SourceError("PROJECT_IMPORT_INCOMPLETE_BATCH")
            # 持久保存逐行来源关联；下次仍读回线上来源键，超时后不盲目重建。
            for record, source in zip(records, batch, strict=True):
                existing[source["来源键"]] = record["record_id"]
            repo.set_meta("project_migration_records:" + name, json.dumps(existing))
            _checkpoint(service)
        hidden = [
            f["field_id"]
            for label, f in schema.items()
            if label in target.get("hidden_fields", [])
        ]
        for view in client.pages(path + "/views"):
            if view["view_name"] == "工作台":
                client.request(
                    "PATCH",
                    path + "/views/" + view["view_id"],
                    json={"property": {"hidden_fields": hidden}},
                )
        results.append(
            {
                "name": name,
                "table_id": table,
                "created_rows": len(pending),
                "source_rows": len(target["records"]),
            }
        )
    repo.set_meta("project_migration_source_hash", plan["source_sha256"])
    return {"tables": results, "complete": True}


def _checkpoint(service):
    if hasattr(service, "checkpoint"):
        service.checkpoint()


def relation_values(records, target_records, id_field, pattern):
    """只连接唯一的明确编号；重复或缺失编号不猜测对象。"""
    index = {}
    for record in target_records:
        identifier = record.get("fields", {}).get(id_field, "")
        if isinstance(identifier, str) and re.fullmatch(pattern, identifier):
            index.setdefault(identifier, []).append(record["record_id"])
    result = {}
    for record in records:
        references = record.get("fields", {}).get("关联编号", "")
        if not isinstance(references, str):
            continue
        ids = {
            value[0]
            for identifier, value in index.items()
            if len(value) == 1
            and identifier in references.split("、")
            and value[0] != record["record_id"]
        }
        if ids:
            result[record["record_id"]] = sorted(ids)
    return result
