"""测试用的内存存储后端与 COS SDK 替身（全程不触网）。"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime, timezone

from app.storage.base import ObjectRef, OpenedStream, StorageNotFound, StorageUnavailable


class MemoryBlobStore:
    """实现 `StorageBackend` 协议的内存后端。

    ``name`` 可配（测试 302 需要 ``cos`` 这个名字 + 预签名能力）；
    ``fail_write`` / ``fail_open`` 用来注入存储故障。
    """

    def __init__(self, *, name: str = "local", presign_base: str | None = None) -> None:
        self.name = name
        self.objects_prefix = "blobs"
        self.presign_base = presign_base
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, dict[str, str]] = {}
        self.fail_write = False
        self.fail_open = False
        self.deleted: list[str] = []

    def key_for(self, file_id: str) -> str:
        return f"blobs/{file_id[2:4]}/{file_id}"

    def write(self, source, key, *, content_type, filename, sha256) -> None:
        if self.fail_write:
            raise StorageUnavailable("simulated storage write failure")
        self.objects[key] = source.read()
        self.meta[key] = {"content_type": content_type, "filename": filename, "sha256": sha256}

    def head(self, key):
        if key not in self.objects:
            return None
        from app.storage.base import StoredHead

        return StoredHead(size=len(self.objects[key]), last_modified=datetime.now(timezone.utc))

    def open_read(self, key, *, start=None, end=None) -> OpenedStream:
        if self.fail_open:
            raise StorageUnavailable("simulated storage read failure")
        if key not in self.objects:
            raise StorageNotFound("object missing")
        data = self.objects[key]
        if start is not None:
            data = data[start : (None if end is None else end + 1)]
        return OpenedStream(chunks=iter([data]) if data else iter([]))

    def delete(self, key) -> None:
        self.objects.pop(key, None)
        self.meta.pop(key, None)
        self.deleted.append(key)

    def presigned_url(self, key, expires_seconds):
        if not self.presign_base:
            return None
        return f"{self.presign_base}/{key}?expires={expires_seconds}"

    def health(self):
        return {"backend": self.name, "ok": True}

    def iter_objects(self, prefix: str, *, limit: int = 1000) -> Iterator[ObjectRef]:
        count = 0
        for key, data in list(self.objects.items()):
            if count >= limit:
                break
            if prefix and not key.startswith(prefix):
                continue
            yield ObjectRef(key=key, size=len(data), last_modified=datetime.now(timezone.utc))
            count += 1


class FakeCosError(Exception):
    """复刻 `CosServiceError` 的两个取值方法。"""

    def __init__(self, status: int | None = None, code: str = "", message: str = "fake cos error") -> None:
        super().__init__(message)
        self._status = status
        self._code = code

    def get_status_code(self):
        return self._status

    def get_error_code(self):
        return self._code


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)
        self.closed = False

    def get_raw_stream(self):
        return self._io

    def read(self, size: int = -1) -> bytes:  # pragma: no cover - 兜底路径
        return self._io.read(size)

    def close(self) -> None:
        self.closed = True


class FakeCosClient:
    """只实现 `CosBlobStore` 用到的那几个方法。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict] = []
        self.deletes: list[str] = []
        self.presign_calls: list[dict] = []
        self.head_bucket_calls = 0
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.raise_on: dict[str, Exception] = {}
        self.list_pages: list[dict] = []

    def _maybe_raise(self, op: str) -> None:
        exc = self.raise_on.get(op)
        if exc is not None:
            raise exc

    def put_object(self, **kwargs):
        self._maybe_raise("put")
        body = kwargs.pop("Body")
        self.puts.append({**kwargs, "body_bytes": body.read()})
        self.objects[kwargs["Key"]] = self.puts[-1]["body_bytes"]
        return {"ETag": '"etag"'}

    def head_object(self, Bucket, Key):
        self._maybe_raise("head")
        if Key not in self.objects:
            raise FakeCosError(404, "NoSuchKey")
        return {
            "Content-Length": str(len(self.objects[Key])),
            "Content-Type": "text/plain",
            "ETag": '"etag"',
            "Last-Modified": "2026-10-20T00:00:00.000Z",
        }

    def get_object(self, Bucket, Key, Range=None):
        self._maybe_raise("get")
        self.get_calls.append({"Key": Key, "Range": Range})
        if Key not in self.objects:
            raise FakeCosError(404, "NoSuchKey")
        data = self.objects[Key]
        if Range:
            assert Range.startswith("bytes=")
            first, _, last = Range[6:].partition("-")
            start = int(first)
            end = int(last) if last else len(data) - 1
            data = data[start : end + 1]
        return {"Body": FakeBody(data), "Content-Length": str(len(data))}

    def delete_object(self, Bucket, Key):
        self._maybe_raise("delete")
        self.objects.pop(Key, None)
        self.deletes.append(Key)
        return {}

    def get_presigned_url(self, **kwargs):
        self.presign_calls.append(kwargs)
        return f"https://cos.example/{kwargs['Key']}?sign=fake&expires={kwargs['Expired']}"

    def head_bucket(self, Bucket):
        self._maybe_raise("head_bucket")
        self.head_bucket_calls += 1
        return {}

    def list_objects(self, Bucket, Prefix="", Marker="", MaxKeys=1000):
        self._maybe_raise("list")
        self.list_calls.append({"Prefix": Prefix, "Marker": Marker, "MaxKeys": MaxKeys})
        if self.list_pages:
            return self.list_pages.pop(0)
        items = [key for key in sorted(self.objects) if key.startswith(Prefix)]
        return {
            "Contents": [
                {"Key": key, "Size": str(len(self.objects[key])), "LastModified": "2026-10-20T00:00:00.000Z"}
                for key in items
            ],
            "IsTruncated": "false",
        }
