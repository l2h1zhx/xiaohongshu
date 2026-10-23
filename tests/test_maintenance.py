import hashlib
from datetime import timedelta

import pytest
from test_core import INITIAL, ready

from koc_insight.maintenance import daily_backup, restore_backup
from koc_insight.repository import Repository


def test_automatic_backups_are_daily_and_retained(system, tmp_path):
    ready(system)
    one = daily_backup(system.repo, INITIAL, keep=2)
    assert one and daily_backup(system.repo, INITIAL) is None
    daily_backup(system.repo, INITIAL + timedelta(days=1), keep=2)
    daily_backup(system.repo, INITIAL + timedelta(days=2), keep=2)
    backups = list((tmp_path / "backups").glob("koc-auto-*.sqlite3"))
    assert len(backups) == 2


def test_restore_does_not_change_backup_or_overwrite_user_file(system, tmp_path):
    ready(system)
    source = tmp_path / "saved.sqlite3"
    system.repo.backup(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "restored.sqlite3"
    assert restore_backup(source, target)["integrity"] == "ok"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    r = Repository(target)
    try:
        assert len(r.rows("SELECT * FROM snapshots")) == 1
    finally:
        r.db.close()
    with pytest.raises(ValueError):
        restore_backup(source, target)
