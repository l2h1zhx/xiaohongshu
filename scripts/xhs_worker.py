"""隔离的只读上游工作进程。stdout只用于父进程JSON协议，不是日志。"""

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import ssl
import subprocess
import sys
from urllib.parse import urljoin, urlsplit


async def main():
    upstream = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(upstream))
    import httpx
    from fastapi import FastAPI
    from source import XHS
    from source.module import generate_auth_token
    from curl_cffi.requests.exceptions import RequestException
    from koc_insight.collector import UPSTREAM_SHA, parse_upstream
    from koc_insight.domain import SourceError
    from koc_insight.urls import note_id, validate_url, public_dns

    sha = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if sha != UPSTREAM_SHA:
        raise ValueError("UPSTREAM_VERSION_MISMATCH")
    bundle = ssl.get_default_verify_paths().cafile
    if not bundle:
        raise ValueError("SYSTEM_CA_MISSING")

    class Sink:
        def write(self, *args, **kwargs):
            pass

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
    xhs.print.func = Sink()
    session = xhs.manager.request_client
    session.verify = bundle
    xhs.manager.download_client.verify = bundle
    original_get = session.get

    async def get(url, *values, **kwargs):
        for _ in range(6):
            url = validate_url(str(url))
            public_dns(urlsplit(url).hostname)
            kwargs.update(verify=bundle, allow_redirects=False)
            try:
                response = await original_get(url, *values, **kwargs)
            except RequestException as e:
                code = getattr(e, "code", None)
                raise SourceError(
                    "SOURCE_TLS_ERROR" if code == 60 else "SOURCE_NETWORK_ERROR",
                    retryable=code != 60,
                    block=code == 60,
                ) from None
            if response.status_code in (401, 403, 429):
                raise SourceError("SOURCE_ACCESS_RESTRICTED", block=True)
            if response.status_code >= 500:
                raise SourceError("SOURCE_5XX", retryable=True)
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("location")
                if not target:
                    raise SourceError("INVALID_REDIRECT")
                url = urljoin(url, target)
                continue
            if len(response.content) > 2 * 1024 * 1024:
                raise SourceError("SOURCE_RESPONSE_TOO_LARGE")
            return response
        raise SourceError("TOO_MANY_REDIRECTS")

    session.get = get
    app = FastAPI()
    xhs.setup_routes(app)
    token = generate_auth_token()
    async with (
        xhs,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, 1024 * 1024)
            if not line:
                break
            try:
                if not line.endswith(b"\n"):
                    raise SourceError("PROTOCOL_TOO_LARGE")
                request = json.loads(line)
                if set(request) != {"url"} or not note_id(request["url"]):
                    raise SourceError("INVALID_REQUEST")
                response = await client.post(
                    "/xhs/detail",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "url": request["url"],
                        "download": False,
                        "check_record": False,
                    },
                )
                captured = datetime.now(UTC).isoformat(timespec="microseconds")
                detail = parse_upstream(response.json())
                result = {"ok": True, "detail": detail.dict(), "captured_at": captured}
            except SourceError as e:
                result = {
                    "ok": False,
                    "error": e.code,
                    "retryable": e.retryable,
                    "block": e.block,
                }
            except Exception:
                result = {
                    "ok": False,
                    "error": "UPSTREAM_INTERNAL_ERROR",
                    "retryable": False,
                    "block": True,
                }
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
