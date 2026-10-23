import json

import httpx
from test_core import ready

from koc_insight.report import build_report
from koc_insight.report_delivery import publish_report


def test_report_attachment_permission_gap_is_visible_and_retry_is_idempotent(
    system, configured, respx_mock, tmp_path
):
    ready(system)
    path = tmp_path / "review.xlsx"
    report = build_report(system.repo, "2026-10", path)
    root = "https://open.feishu.cn/open-apis"
    respx_mock.post(root + "/auth/v3/tenant_access_token/internal").respond(
        200, json={"tenant_access_token": "test-only", "expire": 7200}
    )
    tables = root + "/bitable/v1/apps/test-base/tables"
    respx_mock.get(tables).respond(200, json={"data": {"items": []}})
    create = respx_mock.post(tables).respond(
        200, json={"data": {"table_id": "reports"}}
    )
    upload = respx_mock.post(root + "/drive/v1/medias/upload_all")
    upload.side_effect = [
        httpx.Response(
            400, json={"code": 99991672, "msg": "Required: [bitable:app, drive:drive]"}
        ),
        httpx.Response(
            200, json={"code": 0, "data": {"file_token": "test-file-token"}}
        ),
    ]
    records = tables + "/reports/records"
    respx_mock.post(records + "/search").respond(200, json={"data": {"items": []}})
    added = respx_mock.post(records).respond(
        200, json={"data": {"record": {"record_id": "report-record"}}}
    )
    updated = respx_mock.put(records + "/report-record").respond(200, json={})
    missing = publish_report(system, path, "2026-10", report["delivery_status"])
    assert not missing["excel_available"]
    assert "bitable:app" in missing["attention"]
    publish_report(system, path, "2026-10")
    assert upload.call_count == 1  # 已知缺权限时不每小时重复上传
    success = publish_report(system, path, "2026-10", retry_upload=True)
    assert success["excel_available"] and success["attention"] is None
    assert create.call_count == 1 and added.call_count == 1
    assert json.loads(updated.calls[-1].request.content)["fields"]["月报Excel"] == [
        {"file_token": "test-file-token"}
    ]
    publish_report(system, path, "2026-10")
    assert upload.call_count == 2  # 同一版本复用附件，不重复上传
