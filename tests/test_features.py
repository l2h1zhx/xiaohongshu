from datetime import datetime, timedelta

import pytest
from test_core import NID, PUBLISHED, ready

from koc_insight.analysis import annotate, performance, review
from koc_insight.domain import SH, SourceError
from koc_insight.runtime import tick


def test_annotation_review_preserve_history(system):
    ready(system)
    annotate(system, NID, topic="场景种草", format="图文", selling_points="便携")
    annotate(system, NID, topic="更新分类")
    review(system, NID, "需修改", "补充产品说明")
    assert len(system.repo.rows("SELECT * FROM annotations")) == 2
    assert len(system.repo.rows("SELECT * FROM snapshots")) == 1
    assert system.repo.rows("SELECT * FROM reviews")[0]["decision"] == "需修改"
    with pytest.raises(ValueError):
        review(system, NID, "自动结算")


def test_performance_excludes_late_and_missing(system):
    c = ready(system)
    system.run(c, now=PUBLISHED + timedelta(hours=25), min_interval=0)
    result = performance(system, "2026-10", metric="likes")
    assert result["eligible"] == 1 and result["ranking"][0]["value"] == 20
    assert performance(system, "2026-10", metric="exposure")["eligible"] == 0
    assert performance(system, "2026-10", brand="other")["eligible"] == 0


def test_tick_uses_real_schedule_and_creates_report(system, tmp_path, monkeypatch):
    c = ready(system)

    class Sync:
        def __init__(self, *args):
            pass

        def sync(self, **kwargs):
            return {"synced": 0}

    monkeypatch.setattr("koc_insight.runtime.Feishu", Sync)
    result = tick(
        system,
        collector=c,
        now=datetime(2026, 11, 1, 9, tzinfo=SH),
        min_interval=0,
        report_dir=tmp_path / "reports",
    )
    assert result["processed"] == 2
    assert result["reports"][0]["complete"]
    assert list((tmp_path / "reports").glob("*.xlsx"))
    assert not tick(
        system,
        collector=c,
        now=datetime(2026, 11, 1, 9, tzinfo=SH),
        min_interval=0,
        report_dir=tmp_path / "reports",
    )["reports"]


def test_tick_partial_report_includes_blocked_tasks(system, tmp_path, monkeypatch):
    ready(system)

    class Broken:
        def collect(self, url):
            raise SourceError("SOURCE_ACCESS_RESTRICTED", block=True)

    class Sync:
        def __init__(self, *args):
            pass

        def sync(self, **kwargs):
            return {"synced": 0}

    monkeypatch.setattr("koc_insight.runtime.Feishu", Sync)
    result = tick(
        system,
        collector=Broken(),
        now=datetime(2026, 11, 1, 9, tzinfo=SH),
        min_interval=0,
        report_dir=tmp_path / "reports",
    )
    assert result["reports"] and not result["reports"][0]["complete"]
    assert result["attention_required"]
