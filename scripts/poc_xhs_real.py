"""在 XHS-Downloader 虚拟环境中执行一次授权 PoC，不修改上游源码。

python 此文件 --upstream /path/XHS-Downloader --links /private/links.json --output /private/result.json
输入JSON为用户授权的链接数组。仅详情API，不提供Cookie/代理参数，不下载媒体。
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import ssl
import subprocess
import sys
import traceback
from urllib.parse import urljoin


async def probe(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(args.upstream.resolve()))
    import httpx
    from fastapi import FastAPI
    from source import XHS
    from source.module import generate_auth_token

    from koc_insight.collector import UPSTREAM_SHA, parse_upstream
    from koc_insight.domain import SourceError
    from koc_insight.urls import Resolver, public_dns, validate_url

    head = subprocess.run(
        ["git", "-C", str(args.upstream), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != UPSTREAM_SHA:
        raise ValueError("上游SHA与已审查版本不同")
    links = json.loads(args.links.read_text())
    if (
        not isinstance(links, list)
        or not 1 <= len(links) <= 30
        or not all(isinstance(v, str) for v in links)
    ):
        raise ValueError("仅接受1–30条明确授权链接")
    if args.output.exists():
        raise ValueError("输出已存在，拒绝覆盖")
    xhs = XHS(
        image_download=False,
        video_download=False,
        live_download=False,
        video_cover_download=False,
        download_record=False,
        script_server=False,
        max_retry=0,
        avg_delay=5,
    )

    class PrivateLogSink:
        def write(self, value, **kwargs):
            pass  # 上游使用 rich.Text 与 scroll_end 参数；不持久记录可能含URL的日志。

    xhs.print.func = PrivateLogSink()
    bundle = ssl.get_default_verify_paths().cafile
    if not bundle:
        raise ValueError("没有可用的系统CA信任库")
    session = xhs.manager.request_client
    session.verify = bundle
    xhs.manager.download_client.verify = bundle
    original_get = session.get
    request_status = []

    async def guarded_get(url, *values, **kwargs):
        for _ in range(6):
            url = validate_url(str(url))
            from urllib.parse import urlsplit

            public_dns(urlsplit(url).hostname)
            kwargs["verify"] = bundle
            kwargs["allow_redirects"] = False
            response = await original_get(url, *values, **kwargs)
            request_status.append(response.status_code)
            if response.status_code in (401, 403, 429):
                raise SourceError("SOURCE_ACCESS_RESTRICTED", block=True)
            if response.status_code in (301, 302, 303, 307, 308):
                if not response.headers.get("location"):
                    raise SourceError("INVALID_REDIRECT")
                url = urljoin(url, response.headers["location"])
                continue
            if len(response.content) > 2 * 1024 * 1024:
                raise SourceError("SOURCE_RESPONSE_TOO_LARGE")
            return response
        raise SourceError("TOO_MANY_REDIRECTS")

    session.get = guarded_get
    app = FastAPI()
    xhs.setup_routes(app)
    result = {
        "upstream_sha": head,
        "tls_verification": "system_ca",
        "media_download": False,
        "check_record": False,
        "samples": [],
        "successful_notes": 0,
        "network_statuses": request_status,
    }
    media_extensions = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".gif"}
    volume = args.upstream / "Volume"
    before = {
        str(p)
        for p in volume.rglob("*")
        if p.is_file() and p.suffix.lower() in media_extensions
    }
    token = generate_auth_token()
    async with (
        xhs,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        resolver = Resolver()
        for index, short in enumerate(links, 1):
            item = {"sample": index}
            try:
                resolved = await asyncio.to_thread(resolver.resolve, short)
                response = await client.post(
                    "/xhs/detail",
                    headers={"Authorization": "Bearer " + token},
                    json={"url": resolved, "download": False, "check_record": False},
                )
                item["captured_at"] = datetime.now(UTC).isoformat(
                    timespec="microseconds"
                )
                item["local_api_status"] = response.status_code
                detail = parse_upstream(response.json())
                item.update({"status": "SUCCESS", "detail": detail.dict()})
                result["successful_notes"] += 1
            except SourceError as error:
                item.update(
                    {
                        "status": "BLOCKED" if error.block else "FAILED",
                        "error": error.code,
                    }
                )
                result["samples"].append(item)
                if error.block:
                    result["stopped_on_access_or_unknown_source_error"] = True
                    break
                continue
            except Exception as error:
                item.update({"status": "FAILED", "error_type": type(error).__name__})
                item["error_frames"] = [
                    f"{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}"
                    for frame, line in traceback.walk_tb(error.__traceback__)
                ]
                result["samples"].append(item)
                break  # 不打印可能含签名链接/凭据的异常文本；不盲目重试。
            result["samples"].append(item)
            if index < len(links):
                await asyncio.sleep(5)
        resolver.client.close()
    after = {
        str(p)
        for p in volume.rglob("*")
        if p.is_file() and p.suffix.lower() in media_extensions
    }
    result["new_media_files"] = len(after - before)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.parent.chmod(0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    # stdout仅输出状态与数目，正文与作者信息留在私有结果文件。
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}))
    for item in result["samples"]:
        print(json.dumps({k: v for k, v in item.items() if k != "detail"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--links", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(probe(parser.parse_args()))
