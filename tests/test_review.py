import json

import httpx
from test_core import INITIAL, NID, ready
from test_integrations import mock_feishu

from koc_insight.analysis import review
from koc_insight.feishu import Feishu


def test_metric_refresh_preserves_feishu_manual_review_after_ack(
    system, configured, respx_mock
):
    collector = ready(system)
    review(system, NID, "需修改", "补充品牌说明")
    base, _, _ = mock_feishu(respx_mock)
    respx_mock.post(base + "/records/search").respond(200, json={"data": {"items": []}})
    remote = {}

    def save(request):
        remote.update(json.loads(request.content)["fields"])
        return httpx.Response(
            200, json={"data": {"record": {"record_id": "test-record"}}}
        )

    respx_mock.post(base + "/records").mock(side_effect=save)
    respx_mock.put(base + "/records/test-record").mock(side_effect=save)
    client = Feishu(system.repo)
    client.sync()
    assert remote["审核结论"] == "需修改"
    remote.update({"审核结论": "通过", "审核备注": "已在飞书核对"})
    system.manual(NID, "refresh-after-manual-review", INITIAL)
    system.run(collector, now=INITIAL, min_interval=0)
    client.sync()
    assert remote["审核结论"] == "通过"
    assert remote["审核备注"] == "已在飞书核对"
    review(system, NID, "拒绝", "用户给出新的明确意见")
    client.sync()
    assert remote["审核结论"] == "拒绝"


def test_inspect_preserves_untrusted_content(system):
    ready(system)
    review = system.inspect(NID)
    assert review["detail"]["source"] == "mock"
    assert review["snapshots"][0]["phase"] == "INITIAL"
    assert "不执行" in review["notice"]


def test_outbox_bounded_retries(system, configured, respx_mock):
    ready(system)
    base, _, _ = mock_feishu(respx_mock)
    route = respx_mock.post(base + "/records/search").respond(500)
    f = Feishu(system.repo)
    for _ in range(3):
        f.sync()
        system.repo.db.execute("UPDATE outbox SET next_retry=0")
    assert system.repo.rows("SELECT * FROM outbox")[0]["status"] == "BLOCKED"
    assert f.sync()["synced"] == 0
    assert route.call_count == 3
