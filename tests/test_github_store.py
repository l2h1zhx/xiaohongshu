import base64

import httpx
import pytest

from koc_insight.domain import SourceError
from koc_insight.state import GitHubStore


def test_store_large_file_blob_fallback(monkeypatch, respx_mock):
    monkeypatch.setenv("GH_TOKEN", "test-only")
    client = httpx.Client(base_url="https://api.github.com")
    root = "https://api.github.com/repos/test/project"
    respx_mock.get(root + "/contents/.koc/state.enc").respond(
        200, json={"sha": "abc", "content": ""}
    )
    respx_mock.get(root + "/git/blobs/abc").respond(
        200, json={"content": base64.b64encode(b"encrypted").decode()}
    )
    assert GitHubStore("test/project", client).get("state.enc") == (b"encrypted", "abc")


def test_store_compare_and_swap_rejects_conflict(monkeypatch, respx_mock):
    monkeypatch.setenv("GH_TOKEN", "test-only")
    route = respx_mock.put(
        "https://api.github.com/repos/test/project/contents/.koc/state.enc"
    ).respond(409)
    with pytest.raises(SourceError, match="STATE_CONFLICT"):
        GitHubStore("test/project").put("state.enc", b"cipher", "old-sha")
    import json

    assert json.loads(route.calls[0].request.content)["sha"] == "old-sha"


def test_store_refuses_private_repo_bootstrap(monkeypatch, respx_mock):
    monkeypatch.setenv("GH_TOKEN", "test-only")
    root = "https://api.github.com/repos/test/project"
    respx_mock.get(root + "/git/ref/heads/koc-state").respond(404)
    respx_mock.get(root).respond(200, json={"private": True, "default_branch": "main"})
    with pytest.raises(SourceError, match="FREE_PUBLIC_REPO_REQUIRED"):
        GitHubStore("test/project").ensure_branch()
