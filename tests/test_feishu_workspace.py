import httpx
import pytest

from koc_insight.domain import SourceError
from koc_insight.feishu import FIELD_TYPES, Feishu


def test_private_secret_file_takes_precedence(system, monkeypatch, tmp_path):
    secret = tmp_path / "credential"
    secret.write_text("private-test-value\n")
    monkeypatch.setenv("FEISHU_APP_SECRET", "injected-test-value")
    monkeypatch.setenv("FEISHU_APP_SECRET_FILE", str(secret))
    assert Feishu(system.repo).secret == "private-test-value"


@pytest.mark.parametrize("contents", [None, "", "\n"])
def test_invalid_secret_file_fails_closed(system, monkeypatch, tmp_path, contents):
    secret = tmp_path / "credential"
    if contents is not None:
        secret.write_text(contents)
    monkeypatch.setenv("FEISHU_APP_SECRET", "injected-test-value")
    monkeypatch.setenv("FEISHU_APP_SECRET_FILE", str(secret))
    with pytest.raises(SourceError):
        Feishu(system.repo)


def routes(respx_mock):
    root = "https://open.feishu.cn/open-apis"
    respx_mock.post(root + "/auth/v3/tenant_access_token/internal").respond(
        200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
    )
    create = respx_mock.post(root + "/bitable/v1/apps").respond(
        200,
        json={
            "code": 0,
            "data": {
                "app": {
                    "app_token": "new-base",
                    "url": "https://test.feishu.cn/base/new-base",
                }
            },
        },
    )
    tables = root + "/bitable/v1/apps/new-base/tables"
    respx_mock.get(tables).respond(
        200, json={"code": 0, "data": {"items": [], "has_more": False}}
    )
    respx_mock.post(tables).respond(
        200, json={"code": 0, "data": {"table_id": "new-table"}}
    )
    respx_mock.get(tables + "/new-table/fields").respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [{"field_name": n, "type": t} for n, t in FIELD_TYPES.items()]
            },
        },
    )
    return create


def test_create_base_using_only_app_credentials(
    system, configured, monkeypatch, respx_mock
):
    monkeypatch.delenv("FEISHU_BITABLE_APP_TOKEN")
    monkeypatch.delenv("FEISHU_TABLE_NOTES_ID")
    create = routes(respx_mock)
    result = Feishu(system.repo).initialize_workspace()
    assert result["base_created"]
    assert result["table_id"] == "new-table"
    again = Feishu(system.repo).initialize_workspace()
    assert not again["base_created"]
    assert create.call_count == 1
    assert system.repo.meta("feishu_app_token") == "new-base"


def test_timeout_does_not_blindly_duplicate_base(
    system, configured, monkeypatch, respx_mock
):
    monkeypatch.delenv("FEISHU_BITABLE_APP_TOKEN")
    monkeypatch.delenv("FEISHU_TABLE_NOTES_ID")
    create = routes(respx_mock)
    create.side_effect = httpx.ReadTimeout("test-only-timeout")
    with pytest.raises(SourceError, match="FEISHU_NETWORK_ERROR"):
        Feishu(system.repo).initialize_workspace()
    with pytest.raises(SourceError, match="FEISHU_BASE_CREATE_RESULT_UNCERTAIN"):
        Feishu(system.repo).initialize_workspace()
    assert create.call_count == 1
