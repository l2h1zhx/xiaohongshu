import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .domain import SourceError


def daily_backup(repo, now=None, keep=14):
    now = now or datetime.now(UTC)
    day = now.strftime("%Y-%m-%d")
    if repo.meta("auto_backup_day") == day:
        return None
    root = repo.path.parent / "backups"
    name = f"koc-auto-{day}-{uuid4().hex}.sqlite3"
    repo.backup(root / name)
    history = json.loads(repo.meta("auto_backup_files", "[]"))
    history.append(name)
    removed = history[:-keep]
    for entry in removed:
        if re.fullmatch(r"koc-auto-\d{4}-\d{2}-\d{2}-[0-9a-f]{32}\.sqlite3", entry):
            p = root / entry
            if p.is_file() and not p.is_symlink():
                p.unlink()
    repo.set_meta("auto_backup_files", json.dumps(history[-keep:]))
    repo.set_meta("auto_backup_day", day)
    return str(root / name)


def restore_backup(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise ValueError("备份文件不存在")
    if target.exists():
        raise ValueError("恢复目标已存在，拒绝覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    new = None
    try:
        if (
            old.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or old.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise SourceError("BACKUP_INVALID")
        if not old.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'"
        ).fetchone():
            raise SourceError("BACKUP_INVALID")
        new = sqlite3.connect(target)
        old.backup(new)
    except BaseException:
        if new:
            new.close()
            new = None
        if target.exists():
            target.unlink()
        raise
    finally:
        old.close()
        if new:
            new.close()
    target.chmod(0o600)
    return {"restored": str(target), "integrity": "ok"}
