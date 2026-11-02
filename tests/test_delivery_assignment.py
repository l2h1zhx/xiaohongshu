from datetime import datetime

from koc_insight.domain import SH, stamp


def test_delivery_assignment_preserves_publication_and_plans_assigned_month(system):
    nid = "a" * 24
    published = stamp(datetime(2026, 9, 25, 12, tzinfo=SH))
    submitted = stamp(datetime(2026, 11, 2, 12, tzinfo=SH))
    system.repo.db.execute(
        "INSERT INTO notes(note_id,url,first_submitted,published_at,published_month) VALUES(?,?,?,?,?)",
        (
            nid,
            "https://www.xiaohongshu.com/explore/" + nid,
            submitted,
            published,
            "2026-09",
        ),
    )
    system.repo.db.execute(
        "INSERT INTO delivery_assignments VALUES(?,?,?,?)",
        (nid, "2026-10", "用户指定归入10月交付", submitted),
    )
    system.plan(datetime(2026, 11, 2, 12, tzinfo=SH))
    original = system.repo.rows("SELECT * FROM notes")[0]
    assigned = system.repo.rows("SELECT * FROM delivery_notes")[0]
    assert original["published_month"] == "2026-09"
    assert original["published_at"] == assigned["published_at"] == published
    assert assigned["published_month"] == "2026-10"
    assert assigned["actual_published_month"] == "2026-09"
    assert system.repo.rows("SELECT phase_key FROM jobs WHERE phase='MONTHLY'") == [
        {"phase_key": "2026-10"}
    ]
