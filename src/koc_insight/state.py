"""加密持久状态；公开仓库仅保存密文和不含业务数据的租约。"""

import base64
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .domain import SourceError
from .repository import Repository
from .service import Service

MAGIC = b"KOCSTATE1"
MAX_SIZE = 20 * 1024 * 1024


def key_bytes():
    value = os.getenv("KOC_STATE_KEY")
    if not value and os.getenv("KOC_STATE_KEY_FILE"):
        value = Path(os.environ["KOC_STATE_KEY_FILE"]).read_text().strip()
    try:
        key = base64.b64decode(value or "", validate=True)
    except ValueError:
        raise SourceError("STATE_KEY_INVALID") from None
    if len(key) != 32:
        raise SourceError("STATE_KEY_MISSING_OR_INVALID")
    return key


def generate_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as f:
        f.write(base64.b64encode(secrets.token_bytes(32)).decode())
    path.chmod(0o600)


def semantic_digest(repo):
    content = {}
    ignored = {"worker", "worker_until", "sync_until", "sync_owner", "heartbeat"}
    tables = repo.rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    for entry in tables:
        name = entry["name"]
        if not re.fullmatch(r"[a-z_]+", name):
            raise SourceError("STATE_SCHEMA_INVALID")
        rows = repo.rows(f'SELECT * FROM "{name}"')
        if name == "meta":
            rows = [r for r in rows if r["key"] not in ignored]
        content[name] = sorted(
            rows, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False)
        )
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@contextmanager
def service_context(path="var/koc.sqlite3", local=False):
    if os.getenv("KOC_REMOTE_STATE") == "github" and not local:
        with remote_service() as service:
            yield service
    else:
        repo = Repository(path)
        try:
            yield Service(repo)
        finally:
            repo.db.close()


def seal(repo, report_dir, key):
    with tempfile.TemporaryDirectory() as temp:
        backup = Path(temp) / "state.sqlite3"
        repo.backup(backup)
        stream = io.BytesIO()
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            archive.write(backup, "state.sqlite3")
            root = Path(report_dir)
            if root.exists():
                for p in sorted(root.glob("*.xlsx")):
                    if p.is_symlink():
                        raise SourceError("STATE_REPORT_SYMLINK")
                    archive.write(p, "reports/" + p.name)
        data = stream.getvalue()
    if len(data) > MAX_SIZE:
        raise SourceError("STATE_TOO_LARGE")
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def restore(data, target, key):
    target = Path(target)
    if len(data) > MAX_SIZE + 100 or not data.startswith(MAGIC):
        raise SourceError("STATE_FORMAT_INVALID")
    try:
        plain = AESGCM(key).decrypt(
            data[len(MAGIC) : len(MAGIC) + 12], data[len(MAGIC) + 12 :], MAGIC
        )
    except Exception:
        raise SourceError("STATE_DECRYPT_FAILED") from None
    try:
        with ZipFile(io.BytesIO(plain)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or "state.sqlite3" not in names:
                raise SourceError("STATE_ARCHIVE_INVALID")
            if sum(f.file_size for f in archive.infolist()) > 100 * 1024 * 1024:
                raise SourceError("STATE_ARCHIVE_TOO_LARGE")
            for name in names:
                if name != "state.sqlite3" and not re.fullmatch(
                    r"reports/[A-Za-z0-9_.-]+\.xlsx", name
                ):
                    raise SourceError("STATE_PATH_INVALID")
            for name in names:
                p = target / name
                if p.exists():
                    raise SourceError("STATE_RESTORE_TARGET_EXISTS")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(archive.read(name))
                p.chmod(0o600)
    except SourceError:
        raise
    except Exception:
        raise SourceError("STATE_ARCHIVE_INVALID") from None
    with sqlite3.connect(target / "state.sqlite3") as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise SourceError("STATE_DATABASE_INVALID")


class GitHubStore:
    def __init__(self, repo=None, client=None):
        self.repo = repo or os.getenv("KOC_GITHUB_REPO", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise SourceError("GITHUB_REPO_REQUIRED")
        token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise SourceError("GITHUB_AUTH_REQUIRED")
        self.client = client or httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self.branch = "koc-state"
        self.base = "/repos/" + self.repo

    def request(self, method, path, allow_missing=False, **kwargs):
        try:
            r = self.client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise SourceError("GITHUB_NETWORK_ERROR", retryable=True) from None
        if r.status_code == 404 and allow_missing:
            return None
        if r.status_code in (409, 422):
            raise SourceError("STATE_CONFLICT")
        if r.status_code in (401, 403):
            raise SourceError("GITHUB_ACCESS_DENIED")
        if r.status_code not in (200, 201):
            raise SourceError("GITHUB_API_ERROR")
        return r.json()

    def ensure_branch(self):
        if self.request(
            "GET", self.base + "/git/ref/heads/" + self.branch, allow_missing=True
        ):
            return
        metadata = self.request("GET", self.base)
        if metadata.get("private") is not False:
            raise SourceError("FREE_PUBLIC_REPO_REQUIRED")
        head = self.request(
            "GET", self.base + "/git/ref/heads/" + metadata["default_branch"]
        )
        self.request(
            "POST",
            self.base + "/git/refs",
            json={"ref": "refs/heads/" + self.branch, "sha": head["object"]["sha"]},
        )

    def get(self, path):
        item = self.request(
            "GET",
            self.base + "/contents/.koc/" + path,
            params={"ref": self.branch},
            allow_missing=True,
        )
        if item is None:
            return None, None
        encoded = item.get("content")
        if not encoded:
            item_blob = self.request("GET", self.base + "/git/blobs/" + item["sha"])
            encoded = item_blob["content"]
        data = base64.b64decode(encoded)
        if len(data) > MAX_SIZE + 100:
            raise SourceError("STATE_TOO_LARGE")
        return data, item["sha"]

    def put(self, path, data, sha=None):
        if len(data) > MAX_SIZE + 100:
            raise SourceError("STATE_TOO_LARGE")
        body = {
            "message": "Update encrypted KOC state"
            if path == "state.enc"
            else "Update KOC runtime lease",
            "branch": self.branch,
            "content": base64.b64encode(data).decode(),
        }
        if sha:
            body["sha"] = sha
        return self.request("PUT", self.base + "/contents/.koc/" + path, json=body)[
            "content"
        ]["sha"]


class Lease:
    def __init__(self, store, seconds=900):
        self.store, self.seconds = store, seconds
        self.owner = str(uuid4())
        self.sha = None

    def acquire(self):
        for _ in range(3):
            raw, sha = self.store.get("lease.json")
            if raw and json.loads(raw).get("expires", 0) > time.time():
                raise SourceError("REMOTE_BUSY", retryable=True)
            try:
                self.sha = self.store.put(
                    "lease.json",
                    json.dumps(
                        {"owner": self.owner, "expires": time.time() + self.seconds}
                    ).encode(),
                    sha,
                )
                return
            except SourceError as e:
                if e.code != "STATE_CONFLICT":
                    raise
        raise SourceError("REMOTE_BUSY", retryable=True)

    def renew(self):
        raw, sha = self.store.get("lease.json")
        if (
            not raw
            or json.loads(raw).get("owner") != self.owner
            or json.loads(raw).get("expires", 0) <= time.time()
        ):
            raise SourceError("REMOTE_LEASE_LOST")
        self.sha = self.store.put(
            "lease.json",
            json.dumps(
                {"owner": self.owner, "expires": time.time() + self.seconds}
            ).encode(),
            sha,
        )

    def release(self):
        raw, sha = self.store.get("lease.json")
        if raw and json.loads(raw).get("owner") == self.owner:
            self.store.put(
                "lease.json",
                json.dumps({"owner": self.owner, "expires": 0}).encode(),
                sha,
            )


@contextmanager
def remote_service(store=None, bootstrap=None):
    store = store or GitHubStore()
    key = key_bytes()
    store.ensure_branch()
    lease = Lease(store)
    lease.acquire()
    root = Path(os.getenv("KOC_PRIVATE_DIR", "var/private"))
    root.mkdir(parents=True, exist_ok=True)
    repo = None
    try:
        with tempfile.TemporaryDirectory(dir=root) as temp:
            folder = Path(temp)
            raw, sha = store.get("state.enc")
            if raw is None:
                if bootstrap is None:
                    raise SourceError("REMOTE_STATE_NOT_INITIALIZED")
                source = Repository(bootstrap)
                try:
                    source.backup(folder / "state.sqlite3")
                finally:
                    source.db.close()
            else:
                restore(raw, folder, key)
            repo = Repository(folder / "state.sqlite3")
            service = Service(repo)
            before = semantic_digest(repo)

            def checkpoint():
                nonlocal sha, before
                current = semantic_digest(repo)
                changed = current != before or sha is None
                cipher = seal(repo, folder / "reports", key) if changed else None
                try:
                    lease.renew()
                    if not changed:
                        return
                    sha = store.put("state.enc", cipher, sha)
                except SourceError:
                    if cipher is not None:
                        recovery = root / ("recovery-" + uuid4().hex + ".enc")
                        recovery.write_bytes(cipher)
                        recovery.chmod(0o600)
                    raise
                before = current

            service.checkpoint = checkpoint
            service.report_dir = folder / "reports"
            try:
                yield service
            finally:
                checkpoint()
                repo.db.close()
                repo = None
    finally:
        if repo:
            repo.db.close()
        lease.release()
