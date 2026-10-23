import json
from copy import deepcopy

import pytest
from openpyxl import Workbook

from koc_insight.project_hub import counts, sync_hub
from koc_insight.project_import import import_plan, validate_plan
from koc_insight.project_sources import inspect_workbook


def test_workbook_keeps_hidden_sources_formulas_and_references(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = "任务"
    sheet.append(["ID", "链接", "合计"])
    sheet.append(
        [
            "N001 POST-001",
            '=HYPERLINK("https://xhslink.com/o/test","查看")',
            "=SUM(D2:E2)",
        ]
    )
    hidden = book.create_sheet("历史")
    hidden.sheet_state = "hidden"
    hidden.append(["N001", None])
    hidden["B2"].hyperlink = "https://xhslink.cn/m/account"
    sheet["Z10000"].number_format = "0.00"
    path = tmp_path / "source.xlsx"
    book.save(path)
    result = inspect_workbook(path)
    assert len(result["sheets"]) == 2
    assert result["sheets"][0]["nonempty_rows_including_headers"] == 2
    assert result["sheets"][0]["formulas_without_cache"] == 2
    assert result["sheets"][1]["visibility"] == "hidden"
    assert result["repeated_identifiers"]["N001"] == ["任务!A2", "历史!A1"]
    assert len(result["xhs_link_occurrences"]) == 2
    assert result["notes_verified"] is result["contract_acceptance_verified"] is False


def test_counts_require_source_scope_and_latest_review(system):
    account = "b" * 24
    for n, month, author, source in [
        ("a", "2026-10", account, "xhs-downloader"),
        ("b", "2026-10", account, "mock"),
        ("c", "2026-09", account, "xhs-downloader"),
        ("d", "2026-10", "e" * 24, "xhs-downloader"),
    ]:
        with system.repo.transaction() as db:
            db.execute(
                "INSERT INTO notes(note_id,url,first_submitted,published_month,detail) VALUES(?,?,?,?,?)",
                (
                    n * 24,
                    "https://example.invalid",
                    "now",
                    month,
                    json.dumps({"author_id": author, "source": source}),
                ),
            )
    with system.repo.transaction() as db:
        db.execute(
            "INSERT INTO reviews(note_id,decision,remarks,created_at) VALUES(?,?,?,?)",
            ("a" * 24, "通过", "", "now"),
        )
        db.execute(
            "INSERT INTO reviews(note_id,decision,remarks,created_at) VALUES(?,?,?,?)",
            ("a" * 24, "需修改", "", "later"),
        )
    result = counts(
        system.repo, {"month": "2026-10", "authorized_account_ids": [account]}
    )
    assert result == {
        "已采集笔记": 1,
        "人工审核通过": 0,
        "待审核笔记": 0,
        "其他审核状态笔记": 1,
    }
    assert counts(system.repo, {"month": "2026-10"})["已采集笔记"] is None


def sample_plan():
    return {
        "source_sha256": "a" * 64,
        "tables": [
            {
                "name": "迁移任务",
                "fields": {"编号": 1, "来源键": 1},
                "records": [{"编号": "N001", "来源键": "source:1"}],
            }
        ],
    }


def test_plan_rejects_duplicate_key_and_bad_field_before_writes():
    plan = sample_plan()
    plan["tables"][0]["records"] *= 2
    with pytest.raises(ValueError):
        validate_plan(plan)
    plan = sample_plan()
    plan["tables"][0]["records"][0]["undeclared"] = "bad"
    with pytest.raises(ValueError):
        validate_plan(plan)


def test_import_is_idempotent_and_keeps_manual_changes(system, monkeypatch):
    records = []
    calls = []

    class FakeFeishu:
        app = "base"

        def __init__(self, repo):
            pass

        def pages(self, path):
            if path.endswith("/fields"):
                return [
                    {"field_name": n, "field_id": n, "type": 1}
                    for n in ("编号", "来源键")
                ]
            if path.endswith("/records"):
                return records
            return []

        def request(self, method, path, **kwargs):
            calls.append((method, path))
            if path.endswith("/tables"):
                return {"table_id": "table"}
            if path.endswith("/batch_create"):
                records.extend(
                    {"record_id": str(i), "fields": deepcopy(r["fields"])}
                    for i, r in enumerate(kwargs["json"]["records"])
                )
                return {"records": records}
            raise AssertionError(path)

    monkeypatch.setattr("koc_insight.project_import.Feishu", FakeFeishu)
    first = import_plan(system, sample_plan())
    records[0]["fields"]["编号"] = "手工修改"
    second = import_plan(system, sample_plan())
    assert first["tables"][0]["created_rows"] == 1
    assert second["tables"][0]["created_rows"] == 0
    assert records[0]["fields"]["编号"] == "手工修改"
    assert len(calls) == 2


def test_hub_refuses_switching_existing_project(system):
    system.repo.set_meta("active_project", "original")
    with pytest.raises(ValueError):
        sync_hub(
            system, {"project_id": "replacement", "name": "new", "month": "2026-10"}
        )
    assert system.repo.meta("active_project") == "original"


def test_relation_links_unique_exact_ids_without_guessing():
    from koc_insight.project_import import relation_values

    target = [
        {"record_id": "one", "fields": {"ID": "N001"}},
        {"record_id": "two", "fields": {"ID": "N002"}},
        {"record_id": "three", "fields": {"ID": "N002"}},
    ]
    source = [
        {"record_id": "a", "fields": {"关联编号": "N001、N002、N0010"}},
        {"record_id": "one", "fields": {"关联编号": "N001"}},
    ]
    assert relation_values(source, target, "ID", r"N\d{3}") == {"a": ["one"]}


def test_feishu_pagination_accepts_empty_null_items(system, monkeypatch):
    from koc_insight.feishu import Feishu

    client = Feishu(system.repo)
    monkeypatch.setattr(
        client, "request", lambda *a, **k: {"items": None, "has_more": False}
    )
    assert list(client.pages("/empty")) == []
