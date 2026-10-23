import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .collector import MockCollector
from .domain import Detail, SourceError, parse_time
from .feishu import Feishu, schema
from .report import build_report
from .repository import Repository
from .runtime import IsolatedCollector
from .runtime import tick as runtime_tick
from .service import Service
from .state import (
    generate_key,
    key_bytes,
    remote_service,
    restore,
    seal,
    service_context,
)
from .urls import Resolver


def make_service(path=None):
    return Service(Repository(path or os.getenv("KOC_DB", "var/koc.sqlite3")))


def main():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    parser = argparse.ArgumentParser(description="KOC Insight：默认不进行真实网络写入")
    parser.add_argument("--db", default=os.getenv("KOC_DB", "var/koc.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--brand", default="")
    ingest.add_argument("--text")
    ingest.add_argument("--file", type=Path)
    ingest.add_argument("--requirements", default="")
    commands.add_parser("status")
    commands.add_parser("issues")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("note_id")
    tick = commands.add_parser("tick")
    tick.add_argument("--limit", type=int, default=50)
    commands.add_parser("run-once")
    state_key = commands.add_parser("state-keygen")
    state_key.add_argument("--output", type=Path, required=True)
    commands.add_parser("state-bootstrap")
    unpack = commands.add_parser("state-unpack")
    unpack.add_argument("--file", type=Path, required=True)
    unpack.add_argument("--output-dir", type=Path, required=True)
    pack = commands.add_parser("state-pack")
    pack.add_argument("--output", type=Path, required=True)
    manual = commands.add_parser("refresh")
    manual.add_argument("note_id")
    manual.add_argument("--request-id")
    commands.add_parser("resume-after-review")
    report = commands.add_parser("report")
    report.add_argument("--month", required=True)
    report.add_argument("--brand")
    report.add_argument("--publish", action="store_true")
    report.add_argument("--retry-upload", action="store_true")
    report.add_argument("--output", type=Path, required=True)
    manual_import = commands.add_parser("import-manual")
    manual_import.add_argument("--file", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore_parser = commands.add_parser("restore-backup")
    restore_parser.add_argument("--source", type=Path, required=True)
    restore_parser.add_argument("--output", type=Path, required=True)
    commands.add_parser("feishu-schema")
    commands.add_parser("feishu-diagnose")
    commands.add_parser("feishu-init-table")
    commands.add_parser("feishu-init")
    commands.add_parser("feishu-sync")
    commands.add_parser("feishu-upgrade")
    commands.add_parser("feishu-review-layout")
    brands = commands.add_parser("identify-brands")
    brands.add_argument("--retry-images", action="store_true")
    annotate_parser = commands.add_parser("annotate")
    annotate_parser.add_argument("note_id")
    for name in ("topic", "format", "selling-points", "reuse-advice"):
        annotate_parser.add_argument("--" + name, default="")
    review_parser = commands.add_parser("review")
    review_parser.add_argument("note_id")
    review_parser.add_argument("decision")
    review_parser.add_argument("--remarks", default="")
    ranking = commands.add_parser("performance")
    ranking.add_argument("--month", required=True)
    ranking.add_argument("--brand")
    ranking.add_argument("--metric", default="likes")
    ranking.add_argument("--phase", default="24H")
    commands.add_parser("mcp")
    demo = commands.add_parser("demo")
    demo.add_argument("--output", type=Path, default=Path("var/demo-report.xlsx"))
    args = parser.parse_args()
    if args.command == "state-keygen":
        generate_key(args.output)
        print(json.dumps({"key_file": str(args.output), "key_value_printed": False}))
        return
    if args.command == "restore-backup":
        from .maintenance import restore_backup

        print(json.dumps(restore_backup(args.source, args.output), ensure_ascii=False))
        return
    if args.command == "state-unpack":
        restore(args.file.read_bytes(), args.output_dir, key_bytes())
        print(json.dumps({"restored_dir": str(args.output_dir)}))
        return
    if args.command == "state-bootstrap":
        with remote_service(bootstrap=args.db) as initialized:
            print(
                json.dumps(
                    {"initialized": True, "status": initialized.status()},
                    ensure_ascii=False,
                )
            )
        return
    if args.command == "mcp":
        from .mcp_server import serve

        serve(args.db)
        return
    context = service_context(args.db, local=args.command == "demo")
    service = context.__enter__()
    try:
        if args.command == "ingest":
            content = args.text
            if args.file:
                from .intake import read_links

                content = read_links(args.file)
            result = service.ingest(content or "", args.brand, args.requirements)
        elif args.command == "status":
            result = service.status()
        elif args.command == "issues":
            result = service.issues()
        elif args.command == "inspect":
            result = service.inspect(args.note_id)
        elif args.command == "tick":
            service.resolve_pending(Resolver())
            with IsolatedCollector() as collector:
                result = service.run(collector, args.limit)
        elif args.command == "run-once":
            result = runtime_tick(service)
        elif args.command == "refresh":
            result = {"request_id": service.manual(args.note_id, args.request_id)}
        elif args.command == "resume-after-review":
            service.resume()
            result = {"resumed": True}
        elif args.command == "report":
            result = build_report(service.repo, args.month, args.output, args.brand)
            if hasattr(service, "report_dir"):
                import hashlib
                import shutil

                service.report_dir.mkdir(exist_ok=True)
                filename = (
                    "report-"
                    + hashlib.sha256(args.output.read_bytes()).hexdigest()[:20]
                    + ".xlsx"
                )
                shutil.copy2(args.output, service.report_dir / filename)
            if args.publish:
                from .report_delivery import publish_report

                result["publication"] = publish_report(
                    service,
                    args.output,
                    args.month,
                    result["delivery_status"],
                    args.brand,
                    args.retry_upload,
                )
        elif args.command == "import-manual":
            data = json.loads(args.file.read_text())
            detail = Detail(**data["detail"])
            result = {
                "request_id": service.import_manual(
                    detail, parse_time(data["captured_at"]), data.get("request_id")
                )
            }
        elif args.command == "backup":
            service.repo.backup(args.output)
            result = {"backup": str(args.output)}
        elif args.command == "state-pack":
            if args.output.exists():
                raise ValueError("不覆盖已有加密备份")
            cipher = seal(
                service.repo,
                getattr(
                    service,
                    "report_dir",
                    Path(os.getenv("KOC_REPORT_DIR", "var/reports")),
                ),
                key_bytes(),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(cipher)
            args.output.chmod(0o600)
            result = {"encrypted_backup": str(args.output)}
        elif args.command == "feishu-schema":
            result = {"fields": schema()}
        elif args.command == "feishu-diagnose":
            result = Feishu(service.repo).diagnostic()
        elif args.command == "feishu-init-table":
            result = Feishu(service.repo).initialize_table()
        elif args.command == "feishu-init":
            result = Feishu(service.repo).initialize_workspace()
        elif args.command == "feishu-sync":
            result = Feishu(service.repo).sync()
        elif args.command == "feishu-upgrade":
            result = Feishu(service.repo).upgrade_schema()
        elif args.command == "feishu-review-layout":
            client = Feishu(service.repo)
            client.checkpoint = getattr(service, "checkpoint", lambda: None)
            result = client.upgrade_review_layout()
        elif args.command == "identify-brands":
            from .brands import identify_pending

            result = identify_pending(service, retry_images=args.retry_images)
        elif args.command == "annotate":
            from .analysis import annotate

            result = annotate(
                service,
                args.note_id,
                args.topic,
                args.format,
                args.selling_points,
                args.reuse_advice,
            )
        elif args.command == "review":
            from .analysis import review

            result = review(service, args.note_id, args.decision, args.remarks)
        elif args.command == "performance":
            from .analysis import performance

            result = performance(
                service, args.month, args.brand, args.metric, args.phase
            )
        elif args.command == "demo":
            # 必须使用独立 demo 数据库，禁止污染已有业务库。
            if service.repo.rows("SELECT id FROM submissions LIMIT 1"):
                raise ValueError("demo 需要空数据库，请用 --db var/demo.sqlite3")
            from datetime import timedelta

            from .domain import SH

            first = datetime(2026, 10, 15, 11, tzinfo=SH)
            content = "\n".join(
                f"https://www.xiaohongshu.com/explore/{i:024x}" for i in range(1, 4)
            )
            service.ingest(content, "演示品牌（虚构）", now=first)
            collector = MockCollector()
            service.run(collector, now=first, min_interval=0)
            service.run(collector, now=first + timedelta(hours=23), min_interval=0)
            service.run(
                collector, now=datetime(2026, 11, 1, 9, tzinfo=SH), min_interval=0
            )
            result = build_report(service.repo, "2026-10", args.output)
            result["warning"] = "全部为虚构 Mock 数据，不可对外作为真实报告"
    except (ValueError, SourceError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    finally:
        context.__exit__(*sys.exc_info())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "run-once" and result.get("attention_required"):
        raise SystemExit(2)


def entrypoint():
    try:
        main()
    except SourceError as e:
        print(
            json.dumps({"error": e.code, "retryable": e.retryable}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
