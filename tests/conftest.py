import pytest

from koc_insight.repository import Repository
from koc_insight.service import Service


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_SECRET_FILE", raising=False)
    monkeypatch.setenv("KOC_REMOTE_STATE", "local")
    monkeypatch.delenv("KOC_STATE_KEY", raising=False)
    monkeypatch.delenv("KOC_STATE_KEY_FILE", raising=False)


@pytest.fixture
def system(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    yield Service(repo)
    repo.db.close()


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "FEISHU_APP_ID": "test-app",
        "FEISHU_APP_SECRET": "test-only-value",
        "FEISHU_BITABLE_APP_TOKEN": "test-base",
        "FEISHU_TABLE_NOTES_ID": "test-table",
    }.items():
        monkeypatch.setenv(key, value)
