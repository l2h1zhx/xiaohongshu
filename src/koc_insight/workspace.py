"""精简项目台账：唯一真实笔记作为计数来源，人工合同审核不被采集覆盖。"""

import json

from .domain import SourceError
from .feishu import Feishu


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
    rows = list(client.pages(base + manifest["05 笔记与验收"] + "/records"))
    metrics = workspace_metrics(repo, rows, context["month"])
    repo.set_meta(
        "contract_reviews:" + context["month"],
        json.dumps(metrics["reviews"], ensure_ascii=False),
    )
    target = context.get("goals", {}).get("contract_minimum", 60)
    coverage = []
    for uid in context.get("authorized_account_ids", []):
        entry = json.loads(repo.meta("account_coverage:" + uid, "{}"))
        if entry.get("complete") and entry.get("month") == context["month"]:
            coverage.append(entry.get("checked_at", ""))
    expected = len(context.get("authorized_account_ids", []))
    coverage_text = f"{len(coverage)}/{expected}账号已核对完整列表"
    if coverage and len(coverage) == expected:
        coverage_text += "；核对截至" + min(coverage)
    else:
        coverage_text += "；其余待正常登录核验，不能将已收录数视为全量"
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
                    "已收录笔记",
                    "合同有效笔记",
                    "待合同审核",
                    "合同缺口",
                    "覆盖状态",
                    "当前行动",
                ],
                [],
            ),
            "使用说明": (["项目", "交接指南", "预算边界", "合同底线", "内部目标"], []),
        },
        "02 任务推进": {
            "现在推进": (
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
                [("状态", "isNot", "已完成"), ("状态", "isNot", "已暂停")],
            ),
            "交接与验收": (
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
                    "实际报价",
                    "现金审批",
                    "首发约定",
                    "联系信息",
                    "合作凭证",
                    "主页链接",
                ],
                [("角色", "is", "待合作KOC")],
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
                    "对应账号",
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
            "待合同审核": (
                [
                    "作者昵称",
                    "标题",
                    "SKU",
                    "原创核验",
                    "披露核验",
                    "合同验收",
                    "审核依据",
                    "原笔记链接",
                ],
                [("合同验收", "isNot", "通过"), ("合同验收", "isNot", "不计入")],
            ),
            "合同已通过": (
                ["品牌", "作者昵称", "标题", "合同验收", "审核依据", "原笔记链接"],
                [("合同验收", "is", "通过")],
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
                    *["结案" + k for k in METRIC_NAMES.values()],
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
                            "录入采集时间",
                            "最新采集时间",
                            "最新采集阶段",
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
                    "接收方",
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
                    "笔记数",
                    "KOC数",
                    "待审核数",
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
        # 使用创建时的默认视图，避免额外保留空白“工作台”视图。
        if "工作台" in actual:
            first = next(iter(views))
            client.request(
                "PATCH", path + "/views/" + actual["工作台"], json={"view_name": first}
            )
            actual[first] = actual.pop("工作台")
        for view, (visible, filters) in views.items():
            if view not in actual:
                actual[view] = client.request(
                    "POST",
                    path + "/views",
                    json={"view_name": view, "view_type": "grid"},
                )["view"]["view_id"]
            prop = {
                "hidden_fields": [
                    f["field_id"] for label, f in fields.items() if label not in visible
                ]
            }
            conditions = []
            for label, operator, value in filters:
                field = fields[label]
                options = {
                    o["name"]: o["id"]
                    for o in field.get("property", {}).get("options", [])
                }
                conditions.append(
                    {
                        "field_id": field["field_id"],
                        "operator": operator,
                        "value": json.dumps(
                            [options.get(value, value)], ensure_ascii=False
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
