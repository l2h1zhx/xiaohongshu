"""不调用付费模型；分析建议由用户正在使用的智能体提交并留痕。"""

import json
from datetime import UTC, datetime

from .domain import METRICS, stamp
from .repository import dump


def annotate(service, note_id, topic="", format="", selling_points="", reuse_advice=""):
    if not service.repo.rows("SELECT 1 FROM notes WHERE note_id=?", (note_id,)):
        raise ValueError("笔记不存在")
    data = {
        "topic": topic,
        "format": format,
        "selling_points": selling_points,
        "reuse_advice": reuse_advice,
    }
    if any(not isinstance(v, str) or len(v) > 4000 for v in data.values()):
        raise ValueError("分析字段需为不超过4000字符的文本")
    now = stamp(datetime.now(UTC))
    with service.repo.transaction() as db:
        db.execute(
            "INSERT INTO annotations(note_id,data,created_at,source) VALUES(?,?,?,?)",
            (note_id, dump(data), now, "agent-suggestion"),
        )
        service.repo.audit(now, "ANNOTATED", note_id)
        for row in db.execute(
            "SELECT DISTINCT brand FROM submissions WHERE note_id=?", (note_id,)
        ):
            latest = db.execute(
                "SELECT id FROM snapshots WHERE note_id=? ORDER BY id DESC LIMIT 1",
                (note_id,),
            ).fetchone()
            if latest:
                # 重置同一快照的同步事件，用新分析重建当前字段；不修改快照。
                db.execute(
                    "INSERT INTO outbox(snapshot_id,note_id,brand) VALUES(?,?,?) ON CONFLICT(snapshot_id,brand) DO UPDATE SET status='PENDING',next_retry=0",
                    (latest[0], note_id, row[0]),
                )
    return {
        "saved": True,
        "source": "agent-suggestion",
        "requires_human_judgment": True,
    }


def review(service, note_id, decision, remarks=""):
    if decision not in ("待审核", "通过", "需修改", "拒绝", "已关闭"):
        raise ValueError("未知审核状态")
    if not service.repo.rows("SELECT 1 FROM notes WHERE note_id=?", (note_id,)):
        raise ValueError("笔记不存在")
    now = stamp(datetime.now(UTC))
    with service.repo.transaction() as db:
        db.execute(
            "INSERT INTO reviews(note_id,decision,remarks,created_at) VALUES(?,?,?,?)",
            (note_id, decision, remarks[:10000], now),
        )
        service.repo.audit(now, "OPERATOR_REVIEW", note_id, decision)
        db.execute(
            """INSERT INTO outbox(snapshot_id,note_id,brand)
            SELECT MAX(p.id),p.note_id,s.brand FROM snapshots p JOIN submissions s ON s.note_id=p.note_id
            WHERE p.note_id=? GROUP BY p.note_id,s.brand
            ON CONFLICT(snapshot_id,brand) DO UPDATE SET status='PENDING',next_retry=0""",
            (note_id,),
        )
    return {"decision": decision, "remarks": remarks}


def performance(service, month, brand=None, metric="likes", phase="24H", limit=20):
    from .domain import month_due

    month_due(month)
    if metric not in METRICS or phase not in ("24H", "MONTHLY"):
        raise ValueError("不支持的指标或阶段")
    if not 1 <= limit <= 500:
        raise ValueError("数量需为1–500")
    rows = service.repo.rows(
        "SELECT n.note_id,n.detail FROM notes n WHERE n.published_month=?"
        + (
            " AND EXISTS(SELECT 1 FROM submissions s WHERE s.note_id=n.note_id AND s.brand=?)"
            if brand
            else ""
        ),
        (month, brand) if brand else (month,),
    )
    ranked = []
    missing = 0
    for row in rows:
        snapshots = service.repo.rows(
            "SELECT * FROM snapshots WHERE note_id=? AND phase=?"
            + (" AND phase_key=?" if phase == "MONTHLY" else "")
            + " ORDER BY id DESC LIMIT 1",
            (row["note_id"], phase, month)
            if phase == "MONTHLY"
            else (row["note_id"], phase),
        )
        if not snapshots or (phase == "24H" and snapshots[0]["quality"] != "VALID"):
            missing += 1
            continue
        snap = snapshots[0]
        detail = json.loads(snap["detail"])
        value = detail.get("counts", {}).get(metric)
        if value is None:
            missing += 1
            continue
        annotation = service.repo.rows(
            "SELECT data FROM annotations WHERE note_id=? ORDER BY id DESC LIMIT 1",
            (row["note_id"],),
        )
        ranked.append(
            {
                "note_id": row["note_id"],
                "title": detail.get("title"),
                "value": value,
                "captured_at": snap["captured_at"],
                "quality": snap["quality"],
                "topic": json.loads(annotation[0]["data"]).get("topic")
                if annotation
                else None,
            }
        )
    ranked.sort(key=lambda r: (-r["value"], r["note_id"]))
    return {
        "month": month,
        "metric": metric,
        "phase": phase,
        "eligible": len(ranked),
        "missing_or_ineligible": missing,
        "ranking": ranked[:limit],
        "notice": "仅比较该阶段可用精确值；月结笔记年龄不同，不能据此直接断定选题因果效果。",
    }
