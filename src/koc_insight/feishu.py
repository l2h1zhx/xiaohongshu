"""仅写系统字段；无聊天消息发送、无删除接口。"""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from .domain import METRICS, SourceError, parse_time

TEXT_FIELDS = [
    "业务键",
    "品牌",
    "Note ID",
    "作者ID",
    "作者昵称",
    "标题",
    "正文",
    "话题",
    "类型",
    "数据来源",
    "异常提醒",
    "24H状态",
    "结案状态",
    "人工证据",
]
DATE_FIELDS = [
    "发布时间",
    "录入采集时间",
    "24H采集时间",
    "结案采集时间",
    "人工统计时间",
]
METRIC_NAMES = {
    "likes": "点赞",
    "collects": "收藏",
    "comments": "评论",
    "shares": "分享",
    "exposure": "曝光",
    "reads": "阅读",
}
NUMBER_FIELDS = [
    p + METRIC_NAMES[k] for p in ("录入", "24H", "结案") for k in METRICS
] + ["人工曝光", "人工阅读"]
FIELD_TYPES = {
    **dict.fromkeys(TEXT_FIELDS, 1),
    **dict.fromkeys(DATE_FIELDS, 5),
    **dict.fromkeys(NUMBER_FIELDS, 2),
    "原笔记链接": 15,
}
CORE_FIELD_TYPES = FIELD_TYPES.copy()
EXTENDED_FIELDS = {
    "图片数量": 2,
    **{"最新" + METRIC_NAMES[k]: 2 for k in METRICS},
    "最新采集时间": 5,
    "最新采集阶段": 1,
    "AI选题": 1,
    "AI内容形式": 1,
    "AI核心卖点": 1,
    "AI复用建议": 1,
}
FIELD_TYPES.update(EXTENDED_FIELDS)
REVIEW_FIELDS = {
    **dict.fromkeys(
        ("品牌状态", "品牌识别依据", "审核结论", "审核备注", "数据口径", "关注事项"), 1
    ),
    **dict.fromkeys(tuple(METRIC_NAMES.values()) + ("完整互动量",), 2),
}
EXTENDED_FIELDS.update(REVIEW_FIELDS)
FIELD_TYPES.update(REVIEW_FIELDS)
MAIN_FIELDS = [
    "品牌",
    "品牌状态",
    "作者昵称",
    "标题",
    "类型",
    "发布时间",
    "审核结论",
    "数据口径",
    *METRIC_NAMES.values(),
    "完整互动量",
    "关注事项",
    "原笔记链接",
]


def schema(review=False):
    names = list(FIELD_TYPES)
    if review:
        names = (
            MAIN_FIELDS[:-1]
            + [name for name in names if name not in MAIN_FIELDS]
            + ["原笔记链接"]
        )
    return [{"field_name": name, "type": FIELD_TYPES[name]} for name in names]


class Feishu:
    def __init__(self, repo, client=None):
        self.repo = repo
        self.client = client or httpx.Client(
            base_url="https://open.feishu.cn", timeout=30, follow_redirects=False
        )
        self.token, self.expires = "", 0
        self.app_id = os.getenv("FEISHU_APP_ID", "") or repo.meta("feishu_owner_appid")
        self.secret = os.getenv("FEISHU_APP_SECRET", "")
        secret_file = os.getenv("FEISHU_APP_SECRET_FILE", "")
        if secret_file:
            try:
                self.secret = Path(secret_file).read_text().strip()
            except (OSError, UnicodeError):
                raise SourceError("FEISHU_SECRET_FILE_UNREADABLE") from None
            if not self.secret:
                raise SourceError("FEISHU_CONFIG_MISSING")
        owner = repo.meta("feishu_owner_appid")
        saved = not owner or owner == self.app_id
        self.app = os.getenv("FEISHU_BITABLE_APP_TOKEN", "") or (
            repo.meta("feishu_app_token") if saved else ""
        )
        self.table = os.getenv("FEISHU_TABLE_NOTES_ID", "") or (
            repo.meta("feishu_table_id") if saved else ""
        )
        if saved and repo.meta("feishu_review_table_id"):
            self.table = repo.meta("feishu_review_table_id")

    def business_key(self, nid, brand):
        prefix = (
            self.table + "\0"
            if self.repo.meta("feishu_review_table_id") == self.table
            else ""
        )
        return hashlib.sha256((prefix + brand + "\0" + nid).encode()).hexdigest()

    def auth(self):
        if not all((self.app_id, self.secret)):
            raise SourceError("FEISHU_CONFIG_MISSING")
        if time.time() < self.expires:
            return
        # 官方 MCP 当前工具目录仍列出此自建应用认证接口。
        response = self.client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.secret},
        )
        data = self._decode(response)
        if not data.get("tenant_access_token"):
            raise SourceError("FEISHU_AUTH_ERROR")
        self.token = data["tenant_access_token"]
        self.expires = time.time() + max(0, int(data.get("expire", 0)) - 120)

    @staticmethod
    def _decode(response):
        if response.status_code == 429 or response.status_code >= 500:
            raise SourceError("FEISHU_RETRYABLE", retryable=True)
        if response.status_code in (401, 403):
            raise SourceError("FEISHU_PERMISSION_DENIED")
        if response.status_code != 200:
            raise SourceError("FEISHU_HTTP_ERROR")
        try:
            payload = response.json()
        except ValueError:
            raise SourceError("FEISHU_INVALID_JSON") from None
        if payload.get("code", 0) != 0:
            # 不记录服务端 message，可能含凭证或用户数据。
            raise SourceError("FEISHU_API_ERROR")
        return payload

    def request(self, method, path, **kwargs):
        try:
            self.auth()
            response = self.client.request(
                method,
                path,
                headers={"Authorization": "Bearer " + self.token},
                **kwargs,
            )
            return self._decode(response).get("data", {})
        except httpx.RequestError:
            raise SourceError("FEISHU_NETWORK_ERROR", retryable=True) from None

    @property
    def base(self):
        if not self.app:
            raise SourceError("FEISHU_BITABLE_APP_TOKEN_MISSING")
        if not self.table:
            raise SourceError("FEISHU_TABLE_NOTES_ID_MISSING")
        return f"/open-apis/bitable/v1/apps/{quote(self.app, safe='')}/tables/{quote(self.table, safe='')}"

    def initialize_table(self, name="KOC笔记台账"):
        """在用户指定的多维表格中新建系统表；不修改现有业务表。"""
        if not self.app:
            raise SourceError("FEISHU_BITABLE_APP_TOKEN_MISSING")
        if self.table:
            self.diagnostic()
            return {"table_id": self.table, "created": False}
        path = f"/open-apis/bitable/v1/apps/{quote(self.app, safe='')}/tables"
        matches = [row for row in self.pages(path) if row.get("name") == name]
        if len(matches) > 1:
            raise SourceError("FEISHU_DUPLICATE_TABLE_NAME")
        created = False
        if matches:
            self.table = matches[0]["table_id"]
        else:
            data = self.request(
                "POST",
                path,
                json={
                    "table": {
                        "name": name,
                        "default_view_name": "全部笔记",
                        "fields": schema(),
                    }
                },
            )
            self.table = data["table_id"]
            created = True
        self.diagnostic()
        return {
            "table_id": self.table,
            "created": created,
            "field_count": len(FIELD_TYPES),
        }

    def initialize_workspace(self, name="KOC投放台账"):
        """创建base及系统表，标识持久保存；创建结果不明时禁止盲目重建。"""
        self.auth()
        owner = self.repo.meta("feishu_owner_appid")
        if owner and owner != self.app_id:
            raise SourceError("FEISHU_APP_BINDING_CONFLICT")
        created = False
        if not self.app:
            with self.repo.transaction():
                if self.repo.meta("feishu_base_create_state") in (
                    "CREATING",
                    "UNCERTAIN",
                ):
                    raise SourceError("FEISHU_BASE_CREATE_RESULT_UNCERTAIN")
                saved_app = self.repo.meta("feishu_app_token")
                if saved_app:
                    self.app = saved_app
                else:
                    self.repo.set_meta("feishu_base_create_state", "CREATING")
                    self.repo.set_meta("feishu_owner_appid", self.app_id)
            if not self.app:
                try:
                    data = self.request(
                        "POST",
                        "/open-apis/bitable/v1/apps",
                        json={"name": name, "time_zone": "Asia/Shanghai"},
                    )
                    app = data.get("app", {})
                    if not app.get("app_token"):
                        raise SourceError("FEISHU_BASE_CREATE_INVALID_RESPONSE")
                    self.app = app["app_token"]
                    with self.repo.transaction():
                        self.repo.set_meta("feishu_app_token", self.app)
                        self.repo.set_meta("feishu_app_url", app.get("url", ""))
                        self.repo.set_meta("feishu_base_create_state", "CREATED")
                    created = True
                except SourceError:
                    self.repo.set_meta("feishu_base_create_state", "UNCERTAIN")
                    raise
        result = self.initialize_table()
        with self.repo.transaction():
            self.repo.set_meta("feishu_owner_appid", self.app_id)
            self.repo.set_meta("feishu_app_token", self.app)
            self.repo.set_meta("feishu_table_id", self.table)
        return {
            **result,
            "base_created": created,
            "app_token": self.app,
            "url": self.repo.meta("feishu_app_url") or None,
        }

    def pages(self, path):
        token = None
        seen = set()
        while True:
            data = self.request(
                "GET",
                path,
                params={"page_size": 100, **({"page_token": token} if token else {})},
            )
            yield from data.get("items", [])
            if not data.get("has_more"):
                break
            token = data.get("page_token")
            if not token or token in seen:
                raise SourceError("FEISHU_PAGINATION_ERROR")
            seen.add(token)

    def diagnostic(self):
        actual = {f["field_name"]: f["type"] for f in self.pages(self.base + "/fields")}
        missing = [key for key, kind in FIELD_TYPES.items() if actual.get(key) != kind]
        if missing:
            raise SourceError("FEISHU_SCHEMA_MISMATCH")
        return {"schema": "ok", "required_fields": len(FIELD_TYPES)}

    def upgrade_schema(self):
        """显式添加新版字段；不更改任何已有字段类型，也不删除字段。"""
        actual = {f["field_name"]: f["type"] for f in self.pages(self.base + "/fields")}
        if any(actual.get(k) != v for k, v in CORE_FIELD_TYPES.items()):
            raise SourceError("FEISHU_SCHEMA_MISMATCH")
        added = []
        for name, kind in EXTENDED_FIELDS.items():
            if name in actual:
                if actual[name] != kind:
                    raise SourceError("FEISHU_SCHEMA_MISMATCH")
            else:
                self.request(
                    "POST",
                    self.base + "/fields",
                    json={"field_name": name, "type": kind},
                )
                added.append(name)
        result = {"added_fields": added, **self.diagnostic()}
        if added:
            self.repo.db.execute("""INSERT INTO outbox(snapshot_id,note_id,brand)
                SELECT MAX(p.id),p.note_id,s.brand FROM snapshots p
                JOIN submissions s ON s.note_id=p.note_id GROUP BY p.note_id,s.brand
                ON CONFLICT(snapshot_id,brand) DO UPDATE SET status='PENDING',next_retry=0""")
        return result

    def fields(self, nid, brand):
        from .presentation import note_view

        note = self.repo.rows("SELECT * FROM notes WHERE note_id=?", (nid,))[0]
        detail = json.loads(note["detail"])
        business_key = self.business_key(nid, brand)
        fields = {k: "" for k in TEXT_FIELDS}
        fields.update({k: None for k in DATE_FIELDS + NUMBER_FIELDS})
        fields.update(
            {
                "业务键": business_key,
                "品牌": brand,
                "Note ID": nid,
                "作者ID": detail.get("author_id") or "",
                "作者昵称": detail.get("author_name") or "",
                "标题": (detail.get("title") or "")[:1000],
                "正文": (detail.get("description") or "")[:10000],
                "话题": detail.get("tags") or "",
                "类型": detail.get("note_type") or "",
                "图片数量": detail.get("image_count"),
                "数据来源": detail.get("source") or "",
                "异常提醒": note["warning"] or "",
                "原笔记链接": {"text": "查看原笔记", "link": note["url"]},
            }
        )
        if note["published_at"]:
            fields["发布时间"] = int(
                parse_time(note["published_at"]).timestamp() * 1000
            )
        for phase, label in (("INITIAL", "录入"), ("24H", "24H"), ("MONTHLY", "结案")):
            # MONTHLY 展示最新结案，历史在 SQLite，不修改旧快照。
            snapshots = self.repo.rows(
                "SELECT * FROM snapshots WHERE note_id=? AND phase=? ORDER BY id DESC LIMIT 1",
                (nid, phase),
            )
            if not snapshots:
                if phase == "24H":
                    jobs = self.repo.rows(
                        "SELECT status FROM jobs WHERE note_id=? AND phase='24H'",
                        (nid,),
                    )
                    fields["24H状态"] = jobs[0]["status"] if jobs else "UNKNOWN"
                continue
            snap = snapshots[0]
            values = json.loads(snap["detail"])
            if phase != "24H" or snap["quality"] == "VALID":
                fields.update(
                    {label + METRIC_NAMES[k]: values["counts"].get(k) for k in METRICS}
                )
            fields[label + "采集时间"] = int(
                parse_time(snap["captured_at"]).timestamp() * 1000
            )
            if label + "状态" in FIELD_TYPES:
                fields[label + "状态"] = snap["quality"]
        manuals = self.repo.rows(
            "SELECT * FROM snapshots WHERE note_id=? AND phase='MANUAL' ORDER BY id DESC",
            (nid,),
        )
        for snap in manuals:
            values = json.loads(snap["detail"])
            if values["source"] == "manual":
                fields["人工曝光"] = values["counts"].get("exposure")
                fields["人工阅读"] = values["counts"].get("reads")
                fields["人工统计时间"] = int(
                    parse_time(snap["captured_at"]).timestamp() * 1000
                )
                fields["人工证据"] = values.get("evidence") or ""
                break
        latest = self.repo.rows(
            "SELECT * FROM snapshots WHERE note_id=? AND json_extract(detail,'$.source')!='manual' ORDER BY captured_at DESC,id DESC LIMIT 1",
            (nid,),
        )
        if latest:
            snap = latest[0]
            values = json.loads(snap["detail"])
            fields.update(
                {"最新" + METRIC_NAMES[k]: values["counts"].get(k) for k in METRICS}
            )
            fields["最新采集时间"] = int(
                parse_time(snap["captured_at"]).timestamp() * 1000
            )
            fields["最新采集阶段"] = snap["phase"] + ":" + values["source"]
        annotation = self.repo.rows(
            "SELECT data FROM annotations WHERE note_id=? ORDER BY id DESC LIMIT 1",
            (nid,),
        )
        if annotation:
            values = json.loads(annotation[0]["data"])
            for name, key in (
                ("AI选题", "topic"),
                ("AI内容形式", "format"),
                ("AI核心卖点", "selling_points"),
                ("AI复用建议", "reuse_advice"),
            ):
                fields[name] = values.get(key, "")
        view = note_view(self.repo, nid, brand)
        fields.update(
            {
                "品牌": view["brand"],
                "品牌状态": view["brand_status"],
                "品牌识别依据": view["brand_evidence"][:6000],
                "数据口径": view["basis"],
                "关注事项": "；".join(view["issues"]),
                "完整互动量": view["total"],
                "24H状态": view["observed_status"],
                "结案状态": view["monthly_status"],
                **{METRIC_NAMES[k]: view["counts"].get(k) for k in METRICS},
            }
        )
        # 只推送新的明确审核指令；之后刷新指标不能重放旧结论覆盖飞书手填值。
        reviews = self.repo.rows(
            "SELECT id FROM reviews WHERE note_id=? ORDER BY id DESC LIMIT 1", (nid,)
        )
        acknowledged = int(self.repo.meta("feishu_review_ack:" + business_key, "0"))
        if reviews and reviews[0]["id"] > acknowledged:
            fields.update({"审核结论": view["review"], "审核备注": view["remarks"]})
        return fields

    def configure_review_views(self):
        definitions = {
            "笔记总览": MAIN_FIELDS,
            "24小时表现": ["品牌", "作者昵称", "标题", "发布时间", "24H状态"]
            + ["24H" + METRIC_NAMES[k] for k in METRICS]
            + ["AI选题", "AI复用建议", "原笔记链接"],
            "月结交付": ["品牌", "品牌状态", "作者昵称", "标题", "审核结论", "结案状态"]
            + ["结案" + METRIC_NAMES[k] for k in METRICS]
            + ["关注事项", "原笔记链接"],
            "内容审核": [
                "品牌",
                "品牌状态",
                "作者昵称",
                "标题",
                "正文",
                "话题",
                "发布时间",
                "AI选题",
                "AI核心卖点",
                "AI复用建议",
                "品牌识别依据",
                "审核结论",
                "审核备注",
                "原笔记链接",
            ],
            "后台采集底稿": list(FIELD_TYPES),
        }
        field_ids = {
            f["field_name"]: f["field_id"] for f in self.pages(self.base + "/fields")
        }
        views = {v["view_name"]: v["view_id"] for v in self.pages(self.base + "/views")}
        for name, visible in definitions.items():
            if name not in views:
                created = self.request(
                    "POST",
                    self.base + "/views",
                    json={"view_name": name, "view_type": "grid"},
                )
                views[name] = created["view"]["view_id"]
            hidden = [fid for label, fid in field_ids.items() if label not in visible]
            self.request(
                "PATCH",
                self.base + "/views/" + views[name],
                json={"property": {"hidden_fields": hidden}},
            )
        self.repo.set_meta("feishu_review_default_view", views["笔记总览"])
        return {
            "views": list(definitions),
            "table_id": self.table,
            "view_id": views["笔记总览"],
        }

    def upgrade_review_layout(self):
        """创建业务字段顺序清晰的新表，保留原表和所有已有数据，不删除字段。"""
        old = self.table
        if not self.repo.meta("feishu_review_table_id"):
            tables_path = (
                f"/open-apis/bitable/v1/apps/{quote(self.app, safe='')}/tables"
            )
            matches = [
                t for t in self.pages(tables_path) if t.get("name") == "笔记管理"
            ]
            if matches and not self.repo.meta("feishu_review_create_state"):
                raise SourceError("FEISHU_REVIEW_TABLE_NAME_CONFLICT")
            if not matches:
                if self.repo.meta("feishu_review_create_state") == "UNCERTAIN":
                    raise SourceError("FEISHU_REVIEW_CREATE_UNCERTAIN")
                self.repo.set_meta("feishu_review_create_state", "CREATING")
                if hasattr(self, "checkpoint"):
                    self.checkpoint()
                try:
                    created = self.request(
                        "POST",
                        tables_path,
                        json={
                            "table": {
                                "name": "笔记管理",
                                "default_view_name": "笔记总览",
                                "fields": schema(review=True),
                            }
                        },
                    )
                except SourceError:
                    self.repo.set_meta("feishu_review_create_state", "UNCERTAIN")
                    raise
                self.table = created["table_id"]
            elif len(matches) == 1:
                self.table = matches[0]["table_id"]
            else:
                raise SourceError("FEISHU_REVIEW_TABLE_NAME_CONFLICT")
            self.repo.set_meta("feishu_archive_table_id", old)
            self.repo.set_meta("feishu_review_table_id", self.table)
            self.repo.set_meta("feishu_review_create_state", "DONE")
            self.repo.db.execute("""INSERT INTO outbox(snapshot_id,note_id,brand)
                SELECT MAX(p.id),p.note_id,s.brand FROM snapshots p JOIN submissions s ON s.note_id=p.note_id GROUP BY p.note_id,s.brand
                ON CONFLICT(snapshot_id,brand) DO UPDATE SET status='PENDING',next_retry=0""")
        return self.configure_review_views()

    def sync(self, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError("同步条数必须为 1–500")
        # 独立同步租约避免两个客户端并发创建相同业务行。
        now = time.time()
        owner = str(uuid4())
        with self.repo.transaction():
            if float(self.repo.meta("sync_until", "0")) > now:
                return {"synced": 0, "busy": True}
            self.repo.set_meta("sync_until", str(now + 600))
            self.repo.set_meta("sync_owner", owner)
        synced = 0
        try:
            self.diagnostic()
            for event in self.repo.rows(
                "SELECT * FROM outbox WHERE status IN ('PENDING','RETRY_WAIT') AND next_retry<=? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ):
                with self.repo.transaction():
                    if self.repo.meta("sync_owner") != owner:
                        raise SourceError("SYNC_LEASE_LOST")
                    self.repo.set_meta("sync_until", str(time.time() + 600))
                try:
                    # 重放时重新构建当前字段，旧 outbox 不覆盖最新值。
                    fields = self.fields(event["note_id"], event["brand"])
                    key = fields["业务键"]
                    records = self.repo.rows(
                        "SELECT record_id FROM feishu_records WHERE business_key=?",
                        (key,),
                    )
                    record_id = records[0]["record_id"] if records else None
                    if not record_id:
                        # 写入超时后重放会先查已有业务键，避免盲目再次创建。
                        data = self.request(
                            "POST",
                            self.base + "/records/search",
                            json={
                                "filter": {
                                    "conjunction": "and",
                                    "conditions": [
                                        {
                                            "field_name": "业务键",
                                            "operator": "is",
                                            "value": [key],
                                        }
                                    ],
                                }
                            },
                        )
                        matches = data.get("items", [])
                        if len(matches) > 1 or data.get("has_more"):
                            raise SourceError("FEISHU_DUPLICATE_BUSINESS_KEY")
                        if matches:
                            record_id = matches[0]["record_id"]
                    if record_id:
                        self.request(
                            "PUT",
                            self.base + "/records/" + quote(record_id, safe=""),
                            json={"fields": fields},
                        )
                    else:
                        fields.setdefault("审核结论", "待审核")
                        # 飞书 client_token 要求 UUID4；持久保存后重放沿用同一值。
                        token_key = "feishu_client_token:" + key
                        with self.repo.transaction():
                            client_token = self.repo.meta(token_key)
                            if not client_token:
                                client_token = str(uuid4())
                                self.repo.set_meta(token_key, client_token)
                        data = self.request(
                            "POST",
                            self.base + "/records",
                            json={"fields": fields},
                            params={"client_token": client_token},
                        )
                        record_id = data["record"]["record_id"]
                    with self.repo.transaction() as db:
                        if "审核结论" in fields:
                            review = db.execute(
                                "SELECT id FROM reviews WHERE note_id=? ORDER BY id DESC LIMIT 1",
                                (event["note_id"],),
                            ).fetchone()
                            if review:
                                self.repo.set_meta(
                                    "feishu_review_ack:" + key, str(review[0])
                                )
                        db.execute(
                            "INSERT OR REPLACE INTO feishu_records VALUES(?,?)",
                            (key, record_id),
                        )
                        db.execute(
                            "UPDATE outbox SET status='DONE',attempts=attempts+1,error=NULL WHERE id=?",
                            (event["id"],),
                        )
                    synced += 1
                except SourceError as e:
                    attempts = event["attempts"] + 1
                    retry = e.retryable and attempts < 3
                    self.repo.db.execute(
                        "UPDATE outbox SET attempts=?,status=?,error=?,next_retry=? WHERE id=?",
                        (
                            attempts,
                            "RETRY_WAIT" if retry else "BLOCKED",
                            e.code,
                            time.time() + 300 * attempts,
                            event["id"],
                        ),
                    )
                    break
            return {
                "synced": synced,
                "pending": self.repo.rows(
                    "SELECT count(*) AS n FROM outbox WHERE status!='DONE'"
                )[0]["n"],
            }
        finally:
            with self.repo.transaction():
                if self.repo.meta("sync_owner") == owner:
                    self.repo.set_meta("sync_until", "0")
