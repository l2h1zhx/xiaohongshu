from test_core import NID, ready
from test_integrations import mock_feishu

from koc_insight.feishu import Feishu


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
