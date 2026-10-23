import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS submissions(
 id INTEGER PRIMARY KEY, brand TEXT NOT NULL, url TEXT NOT NULL,
 submitted_at TEXT NOT NULL, note_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING', error TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_retry TEXT, requirements TEXT NOT NULL DEFAULT '',
 UNIQUE(brand,url)
);
CREATE TABLE IF NOT EXISTS notes(
 note_id TEXT PRIMARY KEY, url TEXT NOT NULL, first_submitted TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '{}', published_at TEXT, published_month TEXT,
 warning TEXT
);
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(note_id),
 phase TEXT NOT NULL, phase_key TEXT NOT NULL, due TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_retry TEXT, lease_until TEXT, lease_token TEXT, error TEXT, finished_at TEXT,
 selected_snapshot INTEGER,
 UNIQUE(note_id,phase,phase_key)
);
CREATE TABLE IF NOT EXISTS snapshots(
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
 note_id TEXT NOT NULL REFERENCES notes(note_id), phase TEXT NOT NULL,
 phase_key TEXT NOT NULL, captured_at TEXT NOT NULL, quality TEXT NOT NULL,
 detail TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS snapshots_immutable_update BEFORE UPDATE ON snapshots
 BEGIN SELECT RAISE(ABORT, '历史快照不可修改'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_immutable_delete BEFORE DELETE ON snapshots
 BEGIN SELECT RAISE(ABORT, '历史快照不可删除'); END;
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, note_id TEXT NOT NULL,
 brand TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER DEFAULT 0,
 error TEXT, next_retry REAL NOT NULL DEFAULT 0, UNIQUE(snapshot_id,brand)
);
CREATE TABLE IF NOT EXISTS feishu_records(
 business_key TEXT PRIMARY KEY, record_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, subject TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS annotations(
 id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(note_id),
 data TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(note_id),
 decision TEXT NOT NULL, remarks TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status,due,next_retry);
"""


class Repository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        if "next_retry" not in columns:
            self.db.execute(
                "ALTER TABLE outbox ADD COLUMN next_retry REAL NOT NULL DEFAULT 0"
            )
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def rows(self, sql, values=()):
        return [dict(row) for row in self.db.execute(sql, values)]

    def meta(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def audit(self, at, event, subject, detail=""):
        self.db.execute(
            "INSERT INTO audit(at,event,subject,detail) VALUES(?,?,?,?)",
            (at, event, str(subject), detail),
        )

    def backup(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ValueError("备份目标已存在")
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError("备份完整性检查失败")
        finally:
            target.close()
        path.chmod(0o600)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
