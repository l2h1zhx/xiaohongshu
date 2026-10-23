"""工作进程、计划任务和智能体共用的真实采集入口。"""

import hashlib
import json
import os
import select
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .domain import Detail, SourceError, parse_time
from .feishu import Feishu
from .report import build_report
from .urls import Resolver, note_id, validate_url


class IsolatedCollector:
    def __init__(self, upstream=None):
        self.upstream = Path(
            upstream
            or os.getenv("KOC_UPSTREAM_ROOT", "/workspace/dependencies/XHS-Downloader")
        )
        self.process = None
        self.last_captured_at = None

    def collect(self, url):
        validate_url(url)
        if not note_id(url):
            raise SourceError("RESOLVE_LINK_FIRST")
        if self.process is None:
            python = self.upstream / ".venv/bin/python"
            script = Path(__file__).resolve().parents[2] / "scripts/xhs_worker.py"
            if not python.is_file():
                raise SourceError("UPSTREAM_INSTALL_REQUIRED", block=True)
            self.process = subprocess.Popen(
                [str(python), str(script), str(self.upstream)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env={
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("FEISHU_")
                    and k
                    not in (
                        "GH_TOKEN",
                        "GITHUB_TOKEN",
                        "KOC_STATE_KEY",
                        "KOC_STATE_KEY_FILE",
                    )
                },
            )
        if self.process.poll() is not None:
            raise SourceError("WORKER_EXITED", block=True)
        self.process.stdin.write((json.dumps({"url": url}) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 60
        buffer = bytearray()
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or not select.select([self.process.stdout], [], [], max(0, remaining))[
                    0
                ]
            ):
                self.close()
                raise SourceError("WORKER_TIMEOUT", retryable=True)
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.close()
                raise SourceError("WORKER_EXITED", block=True)
            buffer.extend(chunk)
            if len(buffer) > 2 * 1024 * 1024:
                self.close()
                raise SourceError("WORKER_PROTOCOL_ERROR", block=True)
        try:
            result = json.loads(buffer)
        except ValueError:
            raise SourceError("WORKER_PROTOCOL_ERROR", block=True) from None
        if not result.get("ok"):
            raise SourceError(
                result.get("error", "WORKER_ERROR"),
                retryable=bool(result.get("retryable")),
                block=bool(result.get("block")),
            )
        self.last_captured_at = parse_time(result["captured_at"])
        return Detail(**result["detail"]).validate()

    def close(self):
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            if self.process.stdout:
                self.process.stdout.close()
            self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def tick(
    service,
    limit=200,
    report_dir=None,
    collector=None,
    now=None,
    deadline_seconds=2700,
    min_interval=5,
):
    if not 1 <= limit <= 500:
        raise ValueError("单次任务数必须为1–500")
    started = time.monotonic()
    service.resolve_pending(
        Resolver(),
        now or datetime.now(UTC),
        limit=limit,
        checkpoint=getattr(service, "checkpoint", None),
        deadline=started + deadline_seconds,
    )
    owns = collector is None
    collector = collector or IsolatedCollector()
    processed = 0
    try:
        # 小批执行允许及时检查总时长、备份和同步；不创建空循环。
        while processed < limit and time.monotonic() - started < deadline_seconds:
            result = service.run(
                collector,
                limit=min(10, limit - processed),
                now=now,
                min_interval=min_interval,
            )
            processed += result["processed"]
            if hasattr(service, "checkpoint"):
                service.checkpoint()
            if result.get("circuit") or result.get("busy") or result["processed"] == 0:
                break
    finally:
        if owns:
            collector.close()
    from .brands import identify_pending

    brands = identify_pending(service, limit=min(10, limit))
    sync = Feishu(service.repo).sync(limit=min(500, max(100, limit)))
    reports = []
    root = Path(
        report_dir
        or getattr(service, "report_dir", None)
        or os.getenv("KOC_REPORT_DIR", "var/reports")
    )
    completed = service.repo.rows(
        "SELECT DISTINCT phase_key FROM jobs WHERE phase='MONTHLY'"
    )
    for row in completed:
        month = row["phase_key"]
        # 每月完成后生成一次；以后补录/补采产生新版本。
        tasks = service.repo.rows(
            "SELECT id,status,error,attempts,selected_snapshot FROM jobs WHERE phase='MONTHLY' AND phase_key=? ORDER BY id",
            (month,),
        )
        from .presentation import month_views
        from .review_report import REPORT_VERSION

        version = hashlib.sha256(
            (
                REPORT_VERSION
                + json.dumps(
                    {"tasks": tasks, "review": month_views(service.repo, month)},
                    sort_keys=True,
                )
            ).encode()
        ).hexdigest()[:16]
        key = "report_version:" + month
        if service.repo.meta(key) != str(version):
            path = root / f"KOC-{month}-v{version}.xlsx"
            if not path.exists():
                report = build_report(service.repo, month, path)
                if hasattr(service, "checkpoint"):
                    report.pop("path", None)
                    report["encrypted_file"] = path.name
                reports.append(report)
                report["complete"] = all(t["status"] == "SUCCEEDED" for t in tasks)
                report["unfinished_tasks"] = sum(
                    t["status"] != "SUCCEEDED" for t in tasks
                )
            if service.repo.meta("feishu_report_table_id"):
                from .report_delivery import publish_report

                publication = publish_report(service, path, month)
                if reports and reports[-1].get("month") == month:
                    reports[-1]["publication"] = publication
            service.repo.set_meta(key, str(version))
    return {
        "processed": processed,
        "brand_identification": brands,
        "sync": sync,
        "reports": reports,
        "status": service.status(),
        "backup": _backup(service, now),
        "attention_required": bool(
            service.repo.meta("circuit")
            or service.repo.rows("SELECT 1 FROM outbox WHERE status='BLOCKED' LIMIT 1")
        ),
    }


def _backup(service, now):
    if hasattr(service, "checkpoint"):
        service.checkpoint()
        return {"mode": "encrypted_remote_state"}
    from .maintenance import daily_backup

    return {"mode": "local_daily", "file": daily_backup(service.repo, now)}
