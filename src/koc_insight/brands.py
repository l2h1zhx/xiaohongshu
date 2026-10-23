"""免费品牌识别：明确文字证据优先，图片 OCR 兜底，歧义不猜归属。"""

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .domain import Detail, SourceError, stamp
from .urls import public_dns

AUTO_BRAND = "__AUTO__"
AUTO_LABELS = {AUTO_BRAND, "授权验证（品牌待确认）"}
IGNORE = {
    "SPF",
    "UVA",
    "UVB",
    "PA",
    "ECM",
    "DNA",
    "RNA",
    "AI",
    "KOC",
    "KOL",
    "LED",
    "PDF",
    "APP",
    "DIY",
    "OOTD",
    "TIPS",
    "SKU",
    "VIP",
}


def finding(repo, nid):
    rows = repo.rows(
        "SELECT * FROM brand_findings WHERE note_id=? ORDER BY id DESC LIMIT 1", (nid,)
    )
    return rows[0] if rows else {}


def identity(repo, nid, submitted):
    if submitted not in AUTO_LABELS:
        return submitted, "已指定"
    item = finding(repo, nid)
    return item.get("brand") or "待确认", item.get("status") or "待识别"


def candidates(content, known=(), allow_unlisted_latin=True):
    matches = set()
    for name in known:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])"
        if name and re.search(pattern, content, re.IGNORECASE):
            matches.add(name)
    for name in re.findall(
        r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9]{2,15}(?![A-Za-z0-9-])", content
    ):
        if allow_unlisted_latin and name not in IGNORE and content.count(name) >= 2:
            matches.add(name)
    matches.update(
        re.findall(
            r"品牌\s*[:：]\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{1,15})(?=[\s，。！!；;、]|$)",
            content,
        )
    )
    return sorted(matches)


def record(service, nid, brand, status, evidence, method, image_status="未检查"):
    previous = finding(service.repo, nid)
    values = (brand, status, evidence[:6000], method, image_status)
    if (
        previous
        and tuple(
            previous[k]
            for k in ("brand", "status", "evidence", "method", "image_status")
        )
        == values
    ):
        return previous
    with service.repo.transaction() as db:
        db.execute(
            "INSERT INTO brand_findings(note_id,brand,status,evidence,method,image_status,created_at) VALUES(?,?,?,?,?,?,?)",
            (nid, *values, stamp(datetime.now(UTC))),
        )
        service.repo.audit(stamp(datetime.now(UTC)), "BRAND_IDENTIFIED", nid, status)
        db.execute(
            """INSERT INTO outbox(snapshot_id,note_id,brand)
            SELECT MAX(p.id),p.note_id,s.brand FROM snapshots p JOIN submissions s ON s.note_id=p.note_id
            WHERE p.note_id=? GROUP BY p.note_id,s.brand
            ON CONFLICT(snapshot_id,brand) DO UPDATE SET status='PENDING',next_retry=0""",
            (nid,),
        )
    return finding(service.repo, nid)


def identify_text(service, detail):
    previous = finding(service.repo, detail.note_id)
    if previous.get("method") in ("人工指定", "智能体识别") or (
        previous.get("method", "").startswith("图片OCR") and previous.get("brand")
    ):
        return previous
    content = "\n".join(
        v or "" for v in (detail.title, detail.description, detail.tags)
    )
    known = [
        r["brand"]
        for r in service.repo.rows("SELECT DISTINCT brand FROM submissions")
        if r["brand"] not in AUTO_LABELS
    ]
    names = candidates(content, known)
    if previous.get("method", "").startswith("图片OCR") and not names:
        return previous
    brand = names[0] if len(names) == 1 else ""
    status = "自动识别" if brand else "多品牌待确认" if names else "待确认"
    evidence = (
        "\n".join(
            line for line in content.splitlines() if any(n in line for n in names)
        )
        if names
        else "正文及话题没有明确品牌证据"
    )
    image_status = previous.get("image_status", "未检查")
    if detail.image_urls and image_status == "无可用图片":
        image_status = "未检查"
    return record(
        service, detail.note_id, brand, status, evidence, "正文与话题", image_status
    )


def classify(service, nid, brand, evidence):
    rows = service.repo.rows("SELECT detail FROM notes WHERE note_id=?", (nid,))
    if not rows or not brand.strip() or len(brand) > 80:
        raise ValueError("请提供已采集笔记和有效品牌名")
    detail = json.loads(rows[0]["detail"])
    verified = "\n".join(
        str(detail.get(k) or "") for k in ("title", "description", "tags")
    )
    verified += "\n" + finding(service.repo, nid).get("evidence", "")
    if (
        not evidence.strip()
        or evidence not in verified
        or brand.casefold() not in evidence.casefold()
    ):
        raise ValueError("品牌判断必须引用已采集正文或OCR中的可核查证据")
    return record(service, nid, brand.strip(), "自动识别", evidence, "智能体识别")


def validate_image_url(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if (
            p.scheme not in ("http", "https")
            or p.username
            or p.password
            or p.port not in (None, 443)
            or not (host.endswith(".xhscdn.com") or host.endswith(".xiaohongshu.com"))
        ):
            raise ValueError
    except ValueError:
        raise SourceError("IMAGE_URL_REJECTED") from None
    if any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise SourceError("IMAGE_URL_REJECTED")
    public_dns(host, allowed_hosts={host})
    return p._replace(scheme="https").geturl()


class ImageOCR:
    def read(self, url):
        if not shutil.which("tesseract"):
            raise SourceError("OCR_NOT_INSTALLED")
        url = validate_image_url(url)
        data = bytearray()
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200 or not response.headers.get(
                    "content-type", ""
                ).startswith("image/"):
                    raise SourceError("IMAGE_UNAVAILABLE")
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise SourceError("IMAGE_TOO_LARGE")
        args = ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "11"]
        if os.getenv("KOC_TESSDATA_DIR"):
            args += ["--tessdata-dir", os.environ["KOC_TESSDATA_DIR"]]
        try:
            result = subprocess.run(
                args,
                input=bytes(data),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                env={"PATH": os.defpath, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SourceError("OCR_UNAVAILABLE") from None
        if result.returncode:
            raise SourceError("OCR_UNAVAILABLE")
        return result.stdout.decode("utf-8", errors="replace")[:12000]


def identify_pending(service, reader=None, limit=10, retry_images=False):
    """图片只在内存中短暂处理；每条最多18张，失败留待办，不阻断数据采集。"""
    reader = reader or ImageOCR()
    checked = 0
    for row in service.repo.rows("SELECT note_id,detail FROM notes WHERE detail!='{}'"):
        if checked >= limit:
            break
        detail = Detail(**json.loads(row["detail"]))
        item = identify_text(service, detail)
        # 工作区出口不可达时交给既定的 GitHub 运行器补一次；不无限重试来源拒绝。
        cross_runtime_retry = (
            os.getenv("GITHUB_ACTIONS") == "true"
            and item.get("method") in ("图片OCR", "图片OCR（云工作区）")
            and any(
                code in item.get("image_status", "")
                for code in ("IMAGE_NETWORK_ERROR", "DNS_ERROR")
            )
        )
        if (
            item.get("brand")
            or item.get("status") == "多品牌待确认"
            or (
                item.get("image_status") != "未检查"
                and not retry_images
                and not cross_runtime_retry
            )
        ):
            continue
        checked += 1
        if not detail.image_urls:
            record(
                service,
                detail.note_id,
                "",
                "待确认",
                item.get("evidence", ""),
                "正文与话题",
                "无可用图片",
            )
            continue
        content, failures, names = [], [], []
        for url in detail.image_urls[:18]:
            try:
                content.append(reader.read(url))
            except (SourceError, httpx.RequestError) as error:
                failures.append(
                    error.code
                    if isinstance(error, SourceError)
                    else "IMAGE_NETWORK_ERROR"
                )
        combined = "\n".join(content)
        known = [
            r["brand"]
            for r in service.repo.rows(
                "SELECT DISTINCT brand FROM brand_findings WHERE brand!='' AND method IN ('正文与话题','智能体识别','人工指定')"
            )
        ]
        known += [
            r["brand"]
            for r in service.repo.rows("SELECT DISTINCT brand FROM submissions")
            if r["brand"] not in AUTO_LABELS
        ]
        names = candidates(combined, known, allow_unlisted_latin=False)
        # OCR 是识别建议，不将同图多个产品强行归为一个品牌。
        brand = names[0] if len(names) == 1 else ""
        status = "自动识别" if brand else "多品牌待确认" if names else "待确认"
        image_status = f"已检查{len(content)}/{len(detail.image_urls)}张"
        if failures:
            image_status += "；" + ",".join(sorted(set(failures)))
        record(
            service,
            detail.note_id,
            brand,
            status,
            combined or item.get("evidence", ""),
            "图片OCR（GitHub）"
            if os.getenv("GITHUB_ACTIONS") == "true"
            else "图片OCR（云工作区）",
            image_status,
        )
    return {"checked": checked}
