import json
from datetime import timedelta

from test_core import INITIAL, NID, PUBLISHED, URL, ready

from koc_insight.collector import parse_upstream


def test_image_count_is_metadata_without_retaining_media_urls():
    detail = parse_upstream(
        {
            "data": {
                "作品ID": NID,
                "作品类型": "图文",
                "下载地址": ["https://media.invalid/one", "https://media.invalid/two"],
            }
        }
    )
    assert detail.image_count == 2
    assert "media.invalid" not in json.dumps(detail.dict())


def test_expired_leases_are_bounded(system):
    system.ingest(URL, "A", now=INITIAL)
    for n in range(3):
        assert system._claim(INITIAL + timedelta(minutes=n * 11))
    assert system._claim(INITIAL + timedelta(minutes=33)) is None
    assert system.repo.rows("SELECT status FROM jobs")[0]["status"] == "FAILED"


def test_urgent_24h_is_not_blocked_by_old_late_task(system):
    ready(system)
    other = "https://www.xiaohongshu.com/explore/" + "b" * 24
    system.ingest(other, "A", now=INITIAL)
    system.repo.db.execute(
        "INSERT INTO jobs(note_id,phase,phase_key,due,created_at) VALUES(?,?,?,?,?)",
        (
            "b" * 24,
            "24H",
            "old",
            "2026-10-01T00:00:00.000000+00:00",
            "2026-10-01T00:00:00.000000+00:00",
        ),
    )
    claim = system._claim(PUBLISHED + timedelta(hours=25))
    assert claim["note_id"] == NID and claim["phase"] == "24H"
