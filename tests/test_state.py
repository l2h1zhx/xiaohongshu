import json

import pytest
from test_core import INITIAL, URL, ready

from koc_insight.domain import SourceError
from koc_insight.repository import Repository
from koc_insight.state import (
    Lease,
    generate_key,
    key_bytes,
    remote_service,
    restore,
    seal,
    semantic_digest,
)


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.version = 0
        self.state_writes = 0

    def ensure_branch(self):
        pass

    def get(self, name):
        return self.data.get(name, (None, None))

    def put(self, name, data, sha=None):
        existing = self.data.get(name, (None, None))[1]
        if sha != existing:
            raise SourceError("STATE_CONFLICT")
        self.version += 1
        if name == "state.enc":
            self.state_writes += 1
        value = str(self.version)
        self.data[name] = (data, value)
        return value


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    key = tmp_path / "key.txt"
    generate_key(key)
    monkeypatch.delenv("KOC_STATE_KEY", raising=False)
    monkeypatch.setenv("KOC_STATE_KEY_FILE", str(key))
    monkeypatch.setenv("KOC_PRIVATE_DIR", str(tmp_path / "private"))
    return key_bytes()


def test_encryption_restore_and_tamper(system, tmp_path, keyed):
    ready(system)
    cipher = seal(system.repo, tmp_path / "reports", keyed)
    assert URL.encode() not in cipher
    restore(cipher, tmp_path / "restored", keyed)
    r = Repository(tmp_path / "restored/state.sqlite3")
    try:
        assert semantic_digest(r) == semantic_digest(system.repo)
    finally:
        r.db.close()
    for bad in (cipher[:-1] + bytes([cipher[-1] ^ 1]),):
        with pytest.raises(SourceError, match="STATE_DECRYPT_FAILED"):
            restore(bad, tmp_path / "bad", keyed)
    with pytest.raises(SourceError, match="STATE_DECRYPT_FAILED"):
        restore(cipher, tmp_path / "wrong", b"x" * 32)


def test_key_file_no_overwrite_and_no_print(tmp_path, monkeypatch, capsys):
    p = tmp_path / "key"
    generate_key(p)
    assert p.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        generate_key(p)
    monkeypatch.setenv("KOC_STATE_KEY", "not-base64")
    with pytest.raises(SourceError):
        key_bytes()
    assert capsys.readouterr().out == ""


def test_lease_exclusion_and_owner_safe_release(monkeypatch):
    store = MemoryStore()
    clock = [1000.0]
    monkeypatch.setattr("koc_insight.state.time.time", lambda: clock[0])
    a = Lease(store, seconds=10)
    b = Lease(store, seconds=10)
    a.acquire()
    with pytest.raises(SourceError, match="REMOTE_BUSY"):
        b.acquire()
    clock[0] += 11
    b.acquire()
    a.release()
    assert json.loads(store.get("lease.json")[0])["owner"] == b.owner
    with pytest.raises(SourceError, match="REMOTE_LEASE_LOST"):
        a.renew()
    b.release()


def test_remote_resume_and_no_idle_cipher_churn(system, tmp_path, keyed):
    ready(system)
    store = MemoryStore()
    with remote_service(store, bootstrap=system.repo.path) as s:
        assert len(s.repo.rows("SELECT * FROM snapshots")) == 1
    assert store.state_writes == 1
    with remote_service(store) as s:
        s.repo.set_meta("heartbeat", "new-heartbeat")
    assert store.state_writes == 1
    with remote_service(store) as s:
        s.ingest(URL, "Second brand", now=INITIAL)
        s.checkpoint()
    assert store.state_writes == 2
    with remote_service(store) as s:
        assert len(s.repo.rows("SELECT * FROM submissions")) == 2


def test_missing_remote_state_does_not_create_empty_database(keyed):
    with pytest.raises(SourceError, match="REMOTE_STATE_NOT_INITIALIZED"):
        with remote_service(MemoryStore()):
            pass


def test_failed_checkpoint_preserves_encrypted_recovery(
    system, tmp_path, keyed, monkeypatch
):
    store = MemoryStore()
    with remote_service(store, bootstrap=system.repo.path):
        pass
    original = store.put

    def conflict(name, data, sha=None):
        if name == "state.enc":
            raise SourceError("STATE_CONFLICT")
        return original(name, data, sha)

    store.put = conflict
    with pytest.raises(SourceError, match="STATE_CONFLICT"):
        with remote_service(store) as s:
            s.ingest(URL, "A", now=INITIAL)
    recovery = list((tmp_path / "private").glob("recovery-*.enc"))
    assert recovery and URL.encode() not in recovery[0].read_bytes()


def test_restore_rejects_authenticated_path_traversal(tmp_path, keyed):
    import io
    from zipfile import ZipFile

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from koc_insight.state import MAGIC

    stream = io.BytesIO()
    with ZipFile(stream, "w") as z:
        z.writestr("state.sqlite3", b"not-sqlite")
        z.writestr("../escape", b"bad")
    nonce = b"z" * 12
    data = MAGIC + nonce + AESGCM(keyed).encrypt(nonce, stream.getvalue(), MAGIC)
    with pytest.raises(SourceError, match="STATE_PATH_INVALID"):
        restore(data, tmp_path / "target", keyed)
    assert not (tmp_path / "escape").exists()
