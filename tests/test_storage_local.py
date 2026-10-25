"""本地磁盘后端：原子写、Range、删除幂等、路径穿越防护。"""

from __future__ import annotations

import io

import pytest

from app.config import Settings
from app.storage.base import StorageError, StorageNotFound
from app.storage.local import LocalBlobStore

FILE_ID = "f_" + "A" * 22


def _store(tmp_path) -> LocalBlobStore:
    settings = Settings(
        storage_backend="local",
        local_storage_root=str(tmp_path / "blobs"),
        temp_root=str(tmp_path / "tmp"),
    )
    return LocalBlobStore(settings)


def _write(store, key, data: bytes = b"hello") -> None:
    store.write(io.BytesIO(data), key, content_type="text/plain", filename="x.txt", sha256="deadbeef")


def test_key_layout(tmp_path):
    store = _store(tmp_path)
    key = store.key_for(FILE_ID)
    assert key == f"blobs/AA/{FILE_ID}"


def test_key_for_rejects_bad_id(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(StorageError):
        store.key_for("../../etc/passwd")


def test_write_head_read_delete_roundtrip(tmp_path):
    store = _store(tmp_path)
    key = store.key_for(FILE_ID)
    _write(store, key, b"content-bytes")

    head = store.head(key)
    assert head is not None and head.size == len(b"content-bytes")

    opened = store.open_read(key)
    assert b"".join(opened.chunks) == b"content-bytes"

    store.delete(key)
    assert store.head(key) is None
    # 幂等：再删一次不抛
    store.delete(key)


def test_write_leaves_no_temp_file(tmp_path):
    store = _store(tmp_path)
    key = store.key_for(FILE_ID)
    _write(store, key)
    leftovers = [path.name for path in (tmp_path / "blobs").rglob("*") if ".tmp" in path.name]
    assert leftovers == []


def test_open_read_range(tmp_path):
    store = _store(tmp_path)
    key = store.key_for(FILE_ID)
    _write(store, key, b"0123456789")

    opened = store.open_read(key, start=2, end=5)
    assert b"".join(opened.chunks) == b"2345"

    tail = store.open_read(key, start=7)
    assert b"".join(tail.chunks) == b"789"


def test_open_read_missing(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(StorageNotFound):
        store.open_read(store.key_for(FILE_ID))


def test_path_traversal_is_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(StorageError):
        store.open_read("../outside.txt")
    with pytest.raises(StorageError):
        store.delete("blobs/../../outside.txt")


def test_presigned_url_is_none(tmp_path):
    store = _store(tmp_path)
    assert store.presigned_url(store.key_for(FILE_ID), 300) is None


def test_health_and_iter_objects(tmp_path):
    store = _store(tmp_path)
    assert store.health()["ok"] is True

    key = store.key_for(FILE_ID)
    _write(store, key)
    keys = [ref.key for ref in store.iter_objects("blobs", limit=10)]
    assert keys == [key]
