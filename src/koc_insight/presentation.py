"""审核界面与交付报表共用口径。业务名称与固定快照分开处理。"""

import json
from datetime import UTC, datetime

from .brands import finding, identity
from .domain import month_due

STATE_NAMES = {
    "VALID": "有效",
    "LATE": "晚采（不参加24小时排名）",
    "LATE_ENROLLMENT": "月结后补录",
    "MISSED": "提交较晚，缺少24小时数据",
    "PENDING": "待采集",
    "RETRY_WAIT": "等待重试",
    "FAILED": "采集失败",
    "BLOCKED": "需处理后恢复",
    "RUNNING": "采集中",
    "SUCCEEDED": "已完成",
    "MISSING": "暂无数据",
    "UNKNOWN_PUBLISH_TIME": "发布时间未知",
    "NOT_DUE": "未到月结时间",
}


def total(counts):
    values = [counts.get(k) for k in ("likes", "collects", "comments", "shares")]
    return sum(values) if all(v is not None for v in values) else None


def note_view(repo, nid, association, month=None):
    row = repo.rows("SELECT * FROM notes WHERE note_id=?", (nid,))[0]
    current = json.loads(row["detail"])
    month = month or row["published_month"]
    snapshots = repo.rows(
        "SELECT * FROM snapshots WHERE note_id=? ORDER BY captured_at,id", (nid,)
    )
    public = [s for s in snapshots if json.loads(s["detail"]).get("source") != "manual"]
    observed = next((s for s in reversed(public) if s["phase"] == "24H"), None)
    monthly = next(
        (
            s
            for s in reversed(public)
            if s["phase"] == "MONTHLY" and s["phase_key"] == month
        ),
        None,
    )
    selected = monthly or (public[-1] if public else None)
    counts = json.loads(selected["detail"]).get("counts", {}) if selected else {}
    observed_counts = (
        json.loads(observed["detail"]).get("counts", {})
        if observed and observed["quality"] == "VALID"
        else {}
    )
    jobs = repo.rows("SELECT * FROM jobs WHERE note_id=? AND phase='24H'", (nid,))
    state = (
        observed["quality"]
        if observed
        else jobs[0]["status"]
        if jobs
        else "UNKNOWN_PUBLISH_TIME"
    )
    reviews = repo.rows(
        "SELECT * FROM reviews WHERE note_id=? ORDER BY id DESC LIMIT 1", (nid,)
    )
    annotations = repo.rows(
        "SELECT data FROM annotations WHERE note_id=? ORDER BY id DESC LIMIT 1", (nid,)
    )
    brand, brand_state = identity(repo, nid, association)
    brand_finding = finding(repo, nid)
    evidence = brand_finding.get("evidence", "")
    if brand == "待确认":
        evidence = "正文/话题未提供可确定归属的品牌证据；" + brand_finding.get(
            "image_status", "图片待检查"
        )
    elif brand_finding.get("method", "").startswith("图片OCR"):
        evidence = (
            "图片文字："
            + "；".join(
                line
                for line in evidence.splitlines()
                if brand.casefold() in line.casefold()
            )[:500]
        )
    monthly_state = (
        monthly["quality"]
        if monthly
        else "NOT_DUE"
        if month and month_due(month) > datetime.now(UTC)
        else "MISSING"
    )
    issues = []
    if not brand or brand == "待确认":
        issues.append("品牌待确认")
    if brand_state == "多品牌待确认":
        issues.append("涉及多个品牌，需确认归属")
    if state in ("MISSED", "LATE", "FAILED", "BLOCKED"):
        issues.append(STATE_NAMES[state])
    if monthly_state == "MISSING":
        issues.append("月结数据待补采")
    if monthly_state in ("LATE", "LATE_ENROLLMENT"):
        issues.append("月结为补采值，请核对")
    if row["warning"]:
        issues.append("数据发生变化或缺失，需核对底稿")
    if current.get("source") == "mock":
        issues.append("演示数据，不可交付")
    return {
        "note_id": nid,
        "association": association,
        "brand": brand,
        "brand_status": brand_state,
        "brand_evidence": evidence,
        "url": row["url"],
        "detail": current,
        "published_at": row["published_at"],
        "month": month,
        "selected": selected,
        "counts": counts,
        "basis": "月结数据"
        if monthly
        else "当前数据（非月结）"
        if selected
        else "等待首次采集",
        "total": total(counts),
        "observed": observed,
        "observed_counts": observed_counts,
        "observed_total": total(observed_counts),
        "observed_status": STATE_NAMES.get(state, state),
        "observed_valid": bool(observed and observed["quality"] == "VALID"),
        "monthly": monthly,
        "monthly_status": STATE_NAMES.get(monthly_state, monthly_state),
        "review": reviews[0]["decision"] if reviews else "待审核",
        "remarks": reviews[0]["remarks"] if reviews else "",
        "annotation": json.loads(annotations[0]["data"]) if annotations else {},
        "issues": issues,
    }


def month_views(repo, month, brand=None):
    rows = repo.rows(
        "SELECT DISTINCT n.note_id,s.brand FROM notes n JOIN submissions s ON s.note_id=n.note_id WHERE n.published_month=? ORDER BY n.published_at,n.note_id",
        (month,),
    )
    from .brands import AUTO_BRAND, AUTO_LABELS

    unique = {}
    for row in rows:
        submitted = AUTO_BRAND if row["brand"] in AUTO_LABELS else row["brand"]
        key = (row["note_id"], submitted)
        if key not in unique:
            unique[key] = note_view(repo, row["note_id"], submitted, month)
    result = list(unique.values())
    return [r for r in result if not brand or r["brand"] == brand]
