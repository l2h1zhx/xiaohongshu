"""精简项目台账：唯一真实笔记作为计数来源，人工合同审核不被采集覆盖。"""

import hashlib
import json
from uuid import uuid4

from .domain import SourceError
from .feishu import Feishu

NOTE_TYPES = {
    "标题": 1,
    "品牌": 1,
    "作者昵称": 1,
    "发布时间": 5,
    "正文": 1,
    "SKU": 1,
    "合同验收": 3,
    "审核依据": 1,
    "原创核验": 3,
    "披露核验": 3,
    "点赞": 2,
    "收藏": 2,
    "评论": 2,
    "分享": 2,
    "曝光": 2,
    "阅读": 2,
    "完整互动量": 2,
    "数据口径": 1,
    "关注事项": 1,
    "24H状态": 1,
    "24H点赞": 2,
    "24H收藏": 2,
    "24H评论": 2,
    "24H分享": 2,
    "结案状态": 1,
    "AI选题": 1,
    "AI复用建议": 1,
    "品牌状态": 1,
    "品牌识别依据": 1,
    "历史审核批注": 1,
    "原笔记链接": 15,
    "Note ID": 1,
    "作者ID": 1,
    "业务键": 1,
    "归属月份": 1,
    "发布账号": 18,
    "来源稿件": 1,
}


def coverage_summary(repo, context, month):
    from datetime import UTC, datetime

    from .domain import SH, month_due, parse_time

    expected = set(context.get("authorized_account_ids", []))
    now = datetime.now(UTC)
    end = (
        month_due(month)
        .astimezone(SH)
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    threshold = end if now >= end else parse_time(month + "-01T00:00:00+08:00")
    checked = []
    for uid in expected:
        entry = json.loads(repo.meta("account_coverage:" + uid, "{}"))
        if not entry.get("complete") or entry.get("month") != month:
            continue
        try:
            instant = parse_time(entry["checked_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if threshold <= instant <= now:
            checked.append(entry["checked_at"])
    return {
        "required": bool(expected),
        "verified": len(checked),
        "expected": len(expected),
        "complete": not expected or len(checked) == len(expected),
        "checked_at": min(checked) if checked else None,
    }


def contract_result(fields):
    status = fields.get("合同验收") or "待审核"
    evidence = fields.get("审核依据") or ""
    accepted = status == "通过" and isinstance(evidence, str) and bool(evidence.strip())
    return {"status": status, "evidence": evidence, "accepted": accepted}


def workspace_metrics(repo, rows, month):
    actual = {
        r["note_id"]: json.loads(r["detail"])
        for r in repo.rows(
            "SELECT note_id,detail FROM notes WHERE published_month=? AND detail!='{}'",
            (month,),
        )
        if json.loads(r["detail"]).get("source") == "xhs-downloader"
    }
    seen, accepted, pending, missing_evidence, reviews = set(), 0, 0, 0, {}
    authors = {}
    for row in rows:
        fields = row.get("fields", {})
        nid = fields.get("Note ID")
        if nid not in actual:
            continue
        if nid in seen:
            raise SourceError("WORKSPACE_DUPLICATE_NOTE_ID")
        seen.add(nid)
        review = contract_result(fields)
        reviews[nid] = review
        accepted += review["accepted"]
        pending += not review["accepted"] and review["status"] != "不计入"
        missing_evidence += review["status"] == "通过" and not review["accepted"]
        uid = actual[nid].get("author_id")
        if uid:
            authors[uid] = authors.get(uid, 0) + 1
    if seen != set(actual):
        raise SourceError("WORKSPACE_MISSING_COLLECTED_NOTES")
    return {
        "collected": len(seen),
        "accepted": accepted,
        "pending": pending,
        "missing_approval_evidence": missing_evidence,
        "reviews": reviews,
        "authors": authors,
    }


def sync_workspace(service):
    repo = service.repo
    if repo.meta("workspace_version") != "2":
        return {"updated": False}
    from .project_hub import get_context

    context = get_context(repo)
    manifest = json.loads(repo.meta("project_migration_tables"))
    client = Feishu(repo)
    base = f"/open-apis/bitable/v1/apps/{client.app}/tables/"
    hub_path = (
        base
        + manifest["01 项目总控"]
        + "/records/"
        + repo.meta("project_hub_record_id")
    )
    hub = client.request("GET", hub_path)["record"]["fields"]
    from .domain import month_due

    month = str(hub.get("当前月份", context["month"]))
    month_due(month)
    context["month"] = month
    rows = list(client.pages(base + manifest["05 笔记与验收"] + "/records"))
    periods = {
        r["published_month"]
        for r in repo.rows(
            "SELECT DISTINCT published_month FROM notes WHERE published_month IS NOT NULL"
        )
    } | {month}
    for period in periods:
        result = workspace_metrics(repo, rows, period)
        repo.set_meta(
            "workspace_metrics:" + period, json.dumps(result, ensure_ascii=False)
        )
        repo.set_meta(
            "contract_reviews:" + period,
            json.dumps(result["reviews"], ensure_ascii=False),
        )
    metrics = json.loads(repo.meta("workspace_metrics:" + month))
    try:
        target = int(float(hub["合同底线"]))
        internal = int(float(hub["内部目标"]))
    except (ValueError, TypeError, KeyError):
        raise SourceError("WORKSPACE_INVALID_TARGET") from None
    if target <= 0 or internal <= 0:
        raise SourceError("WORKSPACE_INVALID_TARGET")
    context.setdefault("goals", {}).update(
        {"contract_minimum": target, "internal_goal": internal}
    )
    repo.set_meta(
        "project_context:" + context["project_id"],
        json.dumps(context, ensure_ascii=False),
    )
    coverage = coverage_summary(repo, context, context["month"])
    coverage_text = f"{coverage['verified']}/{coverage['expected']} 已核全量"
    if coverage["required"] and coverage["complete"]:
        coverage_text += "；核对截至" + str(coverage["checked_at"])
    elif coverage["required"]:
        coverage_text += "；" + repo.meta(
            "account_listing_blocked_reason", "待正常登录核验"
        )
    else:
        coverage_text = "按已提供链接收录；未配置账号全量范围"
    fields = {
        "当前月份": context["month"],
        "已收录笔记": metrics["collected"],
        "合同有效笔记": metrics["accepted"],
        "待合同审核": metrics["pending"],
        "合同缺口": max(0, target - metrics["accepted"]),
        "覆盖状态": coverage_text,
    }
    previous = json.loads(repo.meta("workspace_last_hub_fields", "{}"))
    if fields != previous:
        client.request(
            "PUT",
            base
            + manifest["01 项目总控"]
            + "/records/"
            + repo.meta("project_hub_record_id"),
            json={"fields": fields},
        )
        repo.set_meta(
            "workspace_last_hub_fields", json.dumps(fields, ensure_ascii=False)
        )
    people = list(client.pages(base + manifest["03 账号与达人"] + "/records"))
    people_by_uid = {p.get("fields", {}).get("平台账号ID"): p for p in people}
    for uid, count in metrics["authors"].items():
        if uid in people_by_uid:
            continue
        source = next(
            json.loads(n["detail"])
            for n in repo.rows(
                "SELECT detail FROM notes WHERE json_extract(detail,'$.author_id')=?",
                (uid,),
            )
        )
        sequence = (
            max(
                [
                    int(str(p.get("fields", {}).get("编号", ""))[4:])
                    for p in people
                    if str(p.get("fields", {}).get("编号", "")).startswith("PUB-")
                    and str(p.get("fields", {}).get("编号", ""))[4:].isdigit()
                ]
                or [0]
            )
            + 1
        )
        actor_fields = {
            "编号": f"PUB-{sequence:03}",
            "账号/达人": source.get("author_name") or uid,
            "角色": "归属待确认",
            "平台账号ID": uid,
            "负责人": "项目负责人",
            "当前进度": "已读取授权笔记；归属与合作待核对",
            "本月收录笔记": count,
            "下一步": "核对合作关系和合同资格；不推定已签约",
            "主页链接": {
                "text": "查看作者主页",
                "link": "https://www.xiaohongshu.com/user/profile/" + uid,
            },
        }
        token_key = "workspace_actor_token:" + uid
        token = repo.meta(token_key) or str(uuid4())
        repo.set_meta(token_key, token)
        if hasattr(service, "checkpoint"):
            service.checkpoint()
        actor = client.request(
            "POST",
            base + manifest["03 账号与达人"] + "/records",
            params={"client_token": token},
            json={"fields": actor_fields},
        )["record"]
        actor.setdefault("fields", actor_fields)
        people.append(actor)
        people_by_uid[uid] = actor
    note_links = []
    for row in rows:
        nid = row.get("fields", {}).get("Note ID")
        source = (
            repo.rows("SELECT detail FROM notes WHERE note_id=?", (nid,)) if nid else []
        )
        uid = json.loads(source[0]["detail"]).get("author_id") if source else None
        actor = people_by_uid.get(uid)
        if actor and row["fields"].get("发布账号") != [actor["record_id"]]:
            note_links.append(
                {
                    "record_id": row["record_id"],
                    "fields": {"发布账号": [actor["record_id"]]},
                }
            )
    for i in range(0, len(note_links), 100):
        client.request(
            "POST",
            base + manifest["05 笔记与验收"] + "/records/batch_update",
            json={"records": note_links[i : i + 100]},
        )
    updates = []
    for row in people:
        uid = row.get("fields", {}).get("平台账号ID")
        count = metrics["authors"].get(uid, 0)
        current = row.get("fields", {}).get("本月收录笔记")
        # 保存当前月份，字段名不将固定10月写入业务逻辑。
        if current is None or float(current) != count:
            updates.append(
                {"record_id": row["record_id"], "fields": {"本月收录笔记": count}}
            )
    if updates:
        client.request(
            "POST",
            base + manifest["03 账号与达人"] + "/records/batch_update",
            json={"records": updates},
        )
    if repo.meta("workspace_view_month") and repo.meta("workspace_view_month") != month:
        configure_views(client)
        repo.set_meta("workspace_view_month", month)
    return {
        "updated": fields != previous or bool(updates),
        "unique_notes": metrics["collected"],
        "contract_accepted": metrics["accepted"],
        "attention_required": bool(metrics["missing_approval_evidence"]),
    }


def configure_views(client):
    manifest = json.loads(client.repo.meta("project_migration_tables"))
    from .feishu import MAIN_FIELDS, METRIC_NAMES

    definitions = {
        "01 项目总控": {
            "项目总控": (
                [
                    "项目",
                    "当前月份",
                    "合同底线",
                    "内部目标",
                    "已收录笔记",
                    "合同有效笔记",
                    "待合同审核",
                    "合同缺口",
                    "覆盖状态",
                    "当前行动",
                ],
                [],
            ),
            "使用说明": (
                [
                    "项目",
                    "交接指南",
                    "品牌与范围",
                    "合作执行规则",
                    "预算边界",
                    "合同底线",
                    "内部目标",
                ],
                [],
            ),
        },
        "02 任务推进": {
            "P0优先推进": (
                [
                    "任务编号",
                    "执行事项",
                    "负责人",
                    "优先级",
                    "状态",
                    "当前截止",
                    "下一步",
                    "进展与证据",
                ],
                [
                    ("状态", "isNot", "已完成"),
                    ("状态", "isNot", "已暂停"),
                    ("优先级", "is", "P0"),
                ],
            ),
            "全部任务": (
                [
                    "任务编号",
                    "执行事项",
                    "完成标准",
                    "阻塞/风险",
                    "原最早截止",
                    "原任务编号",
                ],
                [],
            ),
            "已完成": (
                ["任务编号", "执行事项", "状态", "进展与证据"],
                [("状态", "is", "已完成")],
            ),
        },
        "03 账号与达人": {
            "账号与达人": (
                [
                    "编号",
                    "账号/达人",
                    "角色",
                    "负责人",
                    "当前进度",
                    "本月收录笔记",
                    "下一步",
                    "主页链接",
                ],
                [],
            ),
            "合作跟进": (
                [
                    "编号",
                    "账号/达人",
                    "当前进度",
                    "合作方式",
                    "粉丝数（原登记）",
                    "内容领域",
                    "实际报价",
                    "现金审批",
                    "首发约定",
                    "联系信息",
                    "合作凭证",
                    "主页链接",
                ],
                [("角色", "is", "外部KOC")],
            ),
            "主体与披露核验": (
                ["编号", "账号/达人", "角色", "核验事项", "平台账号ID", "主页链接"],
                [],
            ),
        },
        "04 内容计划": {
            "内容排期": (
                [
                    "内容编号",
                    "选题",
                    "执行账号",
                    "制作人",
                    "内容状态",
                    "计划发布",
                    "SKU",
                    "下一步",
                    "草稿链接",
                    "发布链接",
                ],
                [],
            ),
            "发布前检查": (
                ["内容编号", "选题", "素材核验", "合规核验", "内容要点", "草稿链接"],
                [],
            ),
        },
        "05 笔记与验收": {
            "笔记总览": (
                [
                    "品牌",
                    "作者昵称",
                    "标题",
                    "发布时间",
                    "SKU",
                    "合同验收",
                    *METRIC_NAMES.values(),
                    "原笔记链接",
                ],
                [],
            ),
            "合同审核": (
                [
                    "作者昵称",
                    "标题",
                    "正文",
                    "SKU",
                    "原创核验",
                    "披露核验",
                    "合同验收",
                    "审核依据",
                    "原笔记链接",
                ],
                [],
            ),
            "24小时表现": (
                [
                    "品牌",
                    "作者昵称",
                    "标题",
                    "24H状态",
                    *["24H" + k for k in METRIC_NAMES.values()],
                    "AI选题",
                    "AI复用建议",
                    "原笔记链接",
                ],
                [],
            ),
            "月结交付": (
                [
                    "品牌",
                    "作者昵称",
                    "标题",
                    "合同验收",
                    "结案状态",
                    *METRIC_NAMES.values(),
                    "数据口径",
                    "原笔记链接",
                ],
                [],
            ),
            "采集底稿": (
                list(
                    dict.fromkeys(
                        MAIN_FIELDS
                        + [
                            "Note ID",
                            "作者ID",
                            "来源稿件",
                            "数据口径",
                            "历史审核批注",
                            "正文",
                            "品牌识别依据",
                        ]
                    )
                ),
                [],
            ),
        },
        "06 产品与寄样": {
            "产品档案": (
                [
                    "记录编号",
                    "产品",
                    "规格",
                    "到货与库存",
                    "可用库存",
                    "合同范围",
                    "可用宣称",
                    "下一步",
                ],
                [("类型", "is", "产品档案")],
            ),
            "实际寄样": (
                [
                    "记录编号",
                    "SKU",
                    "产品",
                    "收件账号",
                    "实际寄出数量",
                    "实际发货日",
                    "物流单号",
                    "签收日",
                    "寄样状态",
                    "审批与凭证",
                ],
                [("类型", "isNot", "产品档案")],
            ),
            "宣传核验": (
                ["SKU", "产品", "合同范围", "可用宣称", "禁用/待核", "审批与凭证"],
                [("类型", "is", "产品档案")],
            ),
        },
        "07 收支结算": {
            "本月收支": (
                [
                    "记录编号",
                    "类型",
                    "服务月",
                    "月份",
                    "合同应收",
                    "实际到账",
                    "到账状态",
                    "已批准支出",
                    "实际支出",
                    "费用审批",
                    "凭证链接",
                ],
                [
                    (
                        "月份",
                        "is",
                        json.loads(
                            client.repo.meta(
                                "project_context:" + client.repo.meta("active_project")
                            )
                        )["month"],
                    )
                ],
            ),
            "合同服务月": (
                ["服务月", "月份", "合同应收", "实际到账", "到账状态", "下一步"],
                [("类型", "is", "服务月结")],
            ),
            "审批与结算规则": (
                ["记录编号", "服务月", "费用审批", "结算待核", "凭证链接"],
                [],
            ),
        },
        "08 月报交付": {
            "月报交付": (
                [
                    "月份",
                    "品牌范围",
                    "交付状态",
                    "已收录笔记",
                    "合同有效笔记",
                    "发布账号数",
                    "待合同审核",
                    "月报Excel",
                ],
                [],
            )
        },
    }
    base = f"/open-apis/bitable/v1/apps/{client.app}/tables/"
    configured = 0
    for name, views in definitions.items():
        path = base + manifest[name]
        fields = {f["field_name"]: f for f in client.pages(path + "/fields")}
        actual = {v["view_name"]: v["view_id"] for v in client.pages(path + "/views")}
        if name == "02 任务推进":
            for old_name, new_name in (
                ("现在推进", "P0优先推进"),
                ("交接与验收", "全部任务"),
            ):
                if old_name in actual and new_name not in actual:
                    client.request(
                        "PATCH",
                        path + "/views/" + actual[old_name],
                        json={"view_name": new_name},
                    )
                    actual[new_name] = actual.pop(old_name)
        # 使用创建时的默认视图，避免额外保留空白“工作台”视图。
        if "工作台" in actual:
            first = next(iter(views))
            client.request(
                "PATCH", path + "/views/" + actual["工作台"], json={"view_name": first}
            )
            actual[first] = actual.pop("工作台")
        if name == "05 笔记与验收":
            for obsolete in ("待合同审核", "合同已通过"):
                if obsolete in actual:
                    client.request("DELETE", path + "/views/" + actual.pop(obsolete))
        for view, (visible, filters) in views.items():
            if view not in actual:
                actual[view] = client.request(
                    "POST",
                    path + "/views",
                    json={"view_name": view, "view_type": "grid"},
                )["view"]["view_id"]
            prop = {
                "hidden_fields": [
                    f["field_id"]
                    for label, f in fields.items()
                    if label not in visible
                    and not f.get("is_primary")
                    and f["field_id"] != next(iter(fields.values()))["field_id"]
                ]
            }
            if name == "05 笔记与验收" and view != "采集底稿" and "归属月份" in fields:
                current_month = json.loads(
                    client.repo.meta(
                        "project_context:" + client.repo.meta("active_project")
                    )
                )["month"]
                filters = filters + [("归属月份", "is", current_month)]
            conditions = []
            for label, operator, value in filters:
                field = fields[label]
                options = {
                    o["name"]: o["id"]
                    for o in (field.get("property") or {}).get("options", [])
                }
                conditions.append(
                    {
                        "field_id": field["field_id"],
                        "field_type": field["type"],
                        "condition_id": "con"
                        + hashlib.sha256(
                            (field["field_id"] + operator + value).encode()
                        ).hexdigest()[:8],
                        "operator": operator,
                        "value": json.dumps(
                            []
                            if operator in ("isEmpty", "isNotEmpty")
                            else [options.get(value, value)],
                            ensure_ascii=False,
                        ),
                    }
                )
            if conditions:
                prop["filter_info"] = {"conjunction": "and", "conditions": conditions}
            client.request(
                "PATCH", path + "/views/" + actual[view], json={"property": prop}
            )
            configured += 1
        if name == "05 笔记与验收":
            client.repo.set_meta("feishu_review_default_view", actual["笔记总览"])
    return {"configured_views": configured}


def review_contract(service, note_id, decision, evidence=""):
    if service.repo.meta("workspace_version") != "2":
        raise ValueError("当前不是精简版工作区")
    if decision not in ("待审核", "待补证", "通过", "不计入"):
        raise ValueError("无效合同审核状态")
    if (
        not isinstance(evidence, str)
        or len(evidence) > 6000
        or decision == "通过"
        and not evidence.strip()
    ):
        raise ValueError("通过须填写用户明确确认的审核依据")
    source = service.repo.rows("SELECT detail FROM notes WHERE note_id=?", (note_id,))
    if not source or json.loads(source[0]["detail"]).get("source") != "xhs-downloader":
        raise ValueError("只能审核已成功采集的真实笔记")
    c = Feishu(service.repo)
    matches = c.request(
        "POST",
        c.base + "/records/search",
        json={
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {"field_name": "Note ID", "operator": "is", "value": [note_id]}
                ],
            }
        },
    )
    if len(matches.get("items") or []) != 1 or matches.get("has_more"):
        raise SourceError("WORKSPACE_NOTE_NOT_UNIQUE")
    row = matches["items"][0]
    c.request(
        "PUT",
        c.base + "/records/" + row["record_id"],
        json={"fields": {"合同验收": decision, "审核依据": evidence}},
    )
    from datetime import UTC, datetime

    from .domain import stamp

    service.repo.audit(stamp(datetime.now(UTC)), "CONTRACT_REVIEW", note_id, decision)
    return {"decision": decision, "workspace": sync_workspace(service)}
