"""COS 后端：用替身客户端验证参数拼装与错误映射（全程不触网、不需要真实凭据）。"""

from __future__ import annotations

import io

import pytest

from app.config import Settings
from app.storage.base import StorageNotFound, StorageUnavailable
from app.storage.cos import CosBlobStore
from tests.fake_storage import FakeCosClient, FakeCosError

FILE_ID = "f_" + "B" * 22


def _settings() -> Settings:
    return Settings(
        storage_backend="cos",
        cos_region="ap-nanjing",
        cos_bucket="elicoudcos-1465229333",
        cos_secret_id="test-id-not-real",
        cos_secret_key="test-key-not-real",
        cos_key_prefix="elicloud-file",
    )


def _store() -> tuple[CosBlobStore, FakeCosClient]:
    fake = FakeCosClient()
    return CosBlobStore(_settings(), client=fake), fake


def test_key_layout():
    store, _ = _store()
    assert store.key_for(FILE_ID) == f"elicloud-file/blobs/BB/{FILE_ID}"


def test_write_sets_private_acl_and_metadata():
    store, fake = _store()
    key = store.key_for(FILE_ID)
    store.write(
        io.BytesIO(b"hello cos"),
        key,
        content_type="text/plain",
        filename="报告.txt",
        sha256="sha256-hex",
    )

    assert len(fake.puts) == 1
    put = fake.puts[0]
    assert put["Bucket"] == "elicoudcos-1465229333"
    assert put["Key"] == key
    assert put["ACL"] == "private"
    assert put["ContentType"] == "text/plain"
    assert "attachment" in put["ContentDisposition"]
    assert "filename*=UTF-8''" in put["ContentDisposition"]
    assert put["Metadata"] == {"x-cos-meta-sha256": "sha256-hex"}
    assert put["body_bytes"] == b"hello cos"


def test_head_present_and_missing():
    store, fake = _store()
    key = store.key_for(FILE_ID)
    fake.objects[key] = b"12345"

    head = store.head(key)
    assert head is not None
    assert head.size == 5
    assert head.content_type == "text/plain"
    assert head.last_modified is not None

    assert store.head("elicloud-file/blobs/ZZ/f_" + "Z" * 22) is None


def test_head_maps_access_denied_to_unavailable():
    store, fake = _store()
    fake.raise_on["head"] = FakeCosError(403, "AccessDenied")
    with pytest.raises(StorageUnavailable):
        store.head(store.key_for(FILE_ID))


def test_open_read_streams_and_forwards_range():
    store, fake = _store()
    key = store.key_for(FILE_ID)
    fake.objects[key] = b"0123456789"

    opened = store.open_read(key, start=2, end=4)
    assert b"".join(opened.chunks) == b"234"
    assert fake.get_calls[-1]["Range"] == "bytes=2-4"

    opened = store.open_read(key, start=7)
    assert b"".join(opened.chunks) == b"789"
    assert fake.get_calls[-1]["Range"] == "bytes=7-"


def test_open_read_missing_is_not_found():
    store, _ = _store()
    with pytest.raises(StorageNotFound):
        store.open_read(store.key_for(FILE_ID))


def test_open_read_403_is_unavailable():
    store, fake = _store()
    fake.raise_on["get"] = FakeCosError(403, "AccessDenied")
    with pytest.raises(StorageUnavailable):
        store.open_read(store.key_for(FILE_ID))


def test_write_failure_is_unavailable():
    store, fake = _store()
    fake.raise_on["put"] = FakeCosError(500, "InternalError")
    with pytest.raises(StorageUnavailable):
        store.write(io.BytesIO(b"x"), store.key_for(FILE_ID), content_type="text/plain", filename="x", sha256="s")


def test_delete_is_idempotent_even_on_404():
    store, fake = _store()
    fake.raise_on["delete"] = FakeCosError(404, "NoSuchKey")
    store.delete(store.key_for(FILE_ID))  # 不抛

    fake.raise_on["delete"] = FakeCosError(403, "AccessDenied")
    with pytest.raises(StorageUnavailable):
        store.delete(store.key_for(FILE_ID))


def test_presigned_url_parameters():
    store, fake = _store()
    key = store.key_for(FILE_ID)
    url = store.presigned_url(key, 300)
    assert url and "sign=fake" in url
    assert fake.presign_calls[-1] == {
        "Bucket": "elicoudcos-1465229333",
        "Key": key,
        "Method": "GET",
        "Expired": 300,
    }


def test_health_is_cached():
    store, fake = _store()
    assert store.health()["ok"] is True
    assert store.health()["ok"] is True
    assert fake.head_bucket_calls == 1


def test_health_failure_is_cached_and_raises():
    store, fake = _store()
    fake.raise_on["head_bucket"] = FakeCosError(403, "AccessDenied")
    with pytest.raises(StorageUnavailable):
        store.health()
    with pytest.raises(StorageUnavailable):
        store.health()
    assert fake.head_bucket_calls == 0


def test_iter_objects_paginates():
    store, fake = _store()
    fake.list_pages = [
        {
            "Contents": [
                {"Key": "elicloud-file/blobs/aa/one", "Size": "1", "LastModified": "2026-10-01T00:00:00.000Z"}
            ],
            "IsTruncated": "true",
            "NextMarker": "elicloud-file/blobs/aa/two",
        },
        {
            "Contents": [
                {"Key": "elicloud-file/blobs/aa/two", "Size": "2", "LastModified": "2026-10-02T00:00:00.000Z"}
            ],
            "IsTruncated": "false",
        },
    ]

    refs = list(store.iter_objects("elicloud-file/blobs", limit=10))
    assert [ref.key for ref in refs] == ["elicloud-file/blobs/aa/one", "elicloud-file/blobs/aa/two"]
    assert fake.list_calls[1]["Marker"] == "elicloud-file/blobs/aa/two"


def test_no_credentials_in_repr_or_health():
    store, _ = _store()
    health = store.health()
    assert "test-key-not-real" not in repr(health)
    assert "test-id-not-real" not in repr(health)
