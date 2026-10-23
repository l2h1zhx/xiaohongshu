"""用上游自身虚拟环境运行：python 此脚本 /path/to/XHS-Downloader。

只探测本机 API 合约，不读取真实笔记，不打印鉴权令牌。
"""

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from source import XHS
from source.module import generate_auth_token


async def main():
    xhs = XHS(image_download=False,video_download=False,live_download=False,
              video_cover_download=False,download_record=False,script_server=False,max_retry=0)
    xhs.manager.request_client.verify = True
    xhs.manager.download_client.verify = True
    app = FastAPI()
    xhs.setup_routes(app)
    async with xhs:
        with TestClient(app) as client:
            schema = client.get("/openapi.json").json()
            body = {"url":"invalid","download":False,"check_record":False}
            denied = client.post("/xhs/detail",json=body)
            response = client.post("/xhs/detail",headers={"Authorization":"Bearer "+generate_auth_token()},json=body)
            result = {
                "route_present":"/xhs/detail" in schema["paths"],
                "unauthenticated_status":denied.status_code,
                "authenticated_invalid_url_status":response.status_code,
                "response_keys":sorted(response.json()),
                "data_is_null":response.json().get("data") is None,
                "request_tls_verified":xhs.manager.request_client.verify,
                "real_notes_collected":0,
            }
            assert result["route_present"] and denied.status_code == 403 and response.status_code == 200
            print(json.dumps(result))


asyncio.run(main())
