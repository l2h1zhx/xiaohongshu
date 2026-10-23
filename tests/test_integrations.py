import json
import os
import subprocess
import sys

import anyio
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_core import INITIAL, NID, URL, ready

from koc_insight.feishu import FIELD_TYPES, Feishu


def mock_feishu(respx_mock):
    root = "https://open.feishu.cn"
    base = root + "/open-apis/bitable/v1/apps/test-base/tables/test-table"
    auth = respx_mock.post(
        root + "/open-apis/auth/v3/tenant_access_token/internal"
    ).respond(
        200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
    )
    fields = respx_mock.get(base + "/fields").respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [
                    {"field_name": name, "type": kind}
                    for name, kind in FIELD_TYPES.items()
                ],
                "has_more": False,
            },
        },
    )
    return base, auth, fields


def test_outbox_replay_and_user_columns(system, configured, respx_mock):
    ready(system)
    base, auth, _ = mock_feishu(respx_mock)
    search = respx_mock.post(base + "/records/search").respond(
        200, json={"code": 0, "data": {"items": []}}
    )
    create = respx_mock.post(base + "/records").respond(
        200, json={"code": 0, "data": {"record": {"record_id": "rec-one"}}}
    )
    update = respx_mock.put(base + "/records/rec-one").respond(
        200, json={"code": 0, "data": {}}
    )
    f = Feishu(system.repo)
    assert f.sync()["synced"] == 1
    system.manual(NID, "refresh", INITIAL)
    from koc_insight.collector import MockCollector

    system.run(MockCollector(), now=INITIAL, min_interval=0)
    assert f.sync()["synced"] == 1
    assert create.call_count == 1
    assert update.call_count == 1
    assert auth.call_count == 1
    assert search.call_count == 1
    fields = json.loads(create.calls[0].request.content)["fields"]
    assert "审核状态" not in fields
    assert fields["原笔记链接"]["link"] == URL
    assert fields["录入曝光"] is None
    assert "client_token" not in json.loads(create.calls[0].request.content)
    assert create.calls[0].request.url.params["client_token"]
    from uuid import UUID

    assert UUID(create.calls[0].request.url.params["client_token"]).version == 4


def test_feishu_failure_preserves_capture(system, configured, respx_mock):
    ready(system)
    base, _, _ = mock_feishu(respx_mock)
    respx_mock.post(base + "/records/search").respond(500)
    result = Feishu(system.repo).sync()
    assert result["pending"] == 1
    assert len(system.repo.rows("SELECT * FROM snapshots")) == 1
    assert (
        system.repo.rows("SELECT * FROM jobs WHERE phase='INITIAL'")[0]["status"]
        == "SUCCEEDED"
    )


def test_feishu_recovers_create_timeout(system, configured, respx_mock):
    ready(system)
    base, _, _ = mock_feishu(respx_mock)
    # 飞书已写入但本地尚无 record_id：先按业务键查找，不再新增。
    respx_mock.post(base + "/records/search").respond(
        200, json={"code": 0, "data": {"items": [{"record_id": "existing"}]}}
    )
    put = respx_mock.put(base + "/records/existing").respond(
        200, json={"code": 0, "data": {}}
    )
    assert Feishu(system.repo).sync()["synced"] == 1
    assert put.call_count == 1


def test_schema_change_stops_writes(system, configured, respx_mock):
    ready(system)
    _base, _, fields = mock_feishu(respx_mock)
    fields.respond(200, json={"code": 0, "data": {"items": []}})
    from koc_insight.domain import SourceError

    with pytest.raises(SourceError, match="FEISHU_SCHEMA_MISMATCH"):
        Feishu(system.repo).sync()
    assert system.repo.rows("SELECT * FROM outbox")[0]["status"] == "PENDING"


def test_initialize_new_table_and_reuse(system, configured, monkeypatch, respx_mock):
    monkeypatch.delenv("FEISHU_TABLE_NOTES_ID")
    base, _, _ = mock_feishu(respx_mock)
    tables = base.rsplit("/", 1)[0]
    listing = respx_mock.get(tables).respond(
        200, json={"code": 0, "data": {"items": [], "has_more": False}}
    )
    created = respx_mock.post(tables).respond(
        200, json={"code": 0, "data": {"table_id": "test-table"}}
    )
    assert Feishu(system.repo).initialize_table()["created"]
    body = json.loads(created.calls[0].request.content)
    assert len(body["table"]["fields"]) == len(FIELD_TYPES)
    listing.respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [{"table_id": "test-table", "name": "KOC笔记台账"}],
                "has_more": False,
            },
        },
    )
    assert not Feishu(system.repo).initialize_table()["created"]
    assert created.call_count == 1


def test_fields_paginated(system, configured, respx_mock):
    _base, _, fields = mock_feishu(respx_mock)
    fields.side_effect = [
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"field_name": "业务键", "type": 1}],
                    "has_more": True,
                    "page_token": "next",
                },
            },
        ),
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {"field_name": name, "type": kind}
                        for name, kind in FIELD_TYPES.items()
                        if name != "业务键"
                    ],
                    "has_more": False,
                },
            },
        ),
    ]
    assert Feishu(system.repo).diagnostic()["schema"] == "ok"
    assert fields.call_count == 2


def test_cli_excel_input(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "input.xlsx"
    w = Workbook()
    w.active.append(["笔记"])
    w.active.append(["点击查看"])
    w.active.cell(2, 1).hyperlink = URL
    w.save(path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "koc_insight",
            "--db",
            str(tmp_path / "input.sqlite3"),
            "ingest",
            "--brand",
            "A",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["accepted"] == 1


def test_actual_mcp_stdio_handshake(tmp_path):
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "koc_insight", "--db", str(tmp_path / "mcp.sqlite3"), "mcp"],
            env={
                **os.environ,
                "XHS_READONLY_REVIEWED": "false",
                "KOC_UPSTREAM_ROOT": str(tmp_path / "not-installed"),
            },
        )
        with anyio.fail_after(20):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    assert {
                        "register_notes",
                        "get_status",
                        "collect_due",
                        "export_month",
                        "get_issues",
                    } <= {t.name for t in tools.tools}
                    registered = await client.call_tool(
                        "register_notes", {"text": URL, "brand": "A"}
                    )
                    assert not registered.isError
                    payload = registered.structuredContent or json.loads(
                        registered.content[0].text
                    )
                    assert payload["accepted"] == 1
                    blocked = await client.call_tool("collect_due", {})
                    assert (
                        blocked.structuredContent or json.loads(blocked.content[0].text)
                    )["blocked"] == "UPSTREAM_INSTALL_REQUIRED"

    anyio.run(exercise)
