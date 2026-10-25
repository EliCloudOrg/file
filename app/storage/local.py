"""本地磁盘后端：测试 / 本地开发用（生产请用 ``STORAGE_BACKEND=cos``）。

写法是「临时文件 + fsync + os.replace」：replace 在同一文件系统内是原子的，
所以下载方永远看不到半截文件，进程崩溃也不会留下「看起来完整」的坏对象。
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from ..constants import CHUNK_SIZE
from .base import (
    ObjectRef,
    OpenedStream,
    StorageError,
    StorageNotFound,
    StorageUnavailable,
    StoredHead,
    validate_file_id,
    validate_key,
)

logger = logging.getLogger(__name__)


class LocalBlobStore:
    name = "local"

    def __init__(self, settings) -> None:
        self.root = Path(settings.local_storage_root)
        self.objects_prefix = "blobs"
        self._ensure_root()

    # ------------------------------------------------------------------ 内部
    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(f"本地存储目录不可创建：{exc}") from exc

    def _path(self, key: str) -> Path:
        cleaned = validate_key(key)
        root = self.root.resolve()
        candidate = (root / cleaned).resolve()
        if candidate != root and root not in candidate.parents:
            # 理论上 key 由我们自己生成；这里再兜一层，防止 DB 被写坏后穿越目录
            raise StorageError("对象 key 越出了存储根目录")
        return candidate

    # ------------------------------------------------------------- StorageBackend
    def key_for(self, file_id: str) -> str:
        identifier = validate_file_id(file_id)
        return f"blobs/{identifier[2:4]}/{identifier}"

    def write(
        self,
        source: BinaryIO,
        key: str,
        *,
        content_type: str,
        filename: str,
        sha256: str,
    ) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(f"本地存储目录不可写：{exc}") from exc

        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with tmp.open("wb") as handle:
                shutil.copyfileobj(source, handle, CHUNK_SIZE)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageUnavailable(f"本地存储写入失败：{exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # content_type / filename 不落盘：真源是数据库；这里只保存内容本身
        del content_type, filename, sha256

    def head(self, key: str) -> StoredHead | None:
        path = self._path(key)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - 权限/IO 异常在正常部署里不该出现
            raise StorageUnavailable(f"本地存储不可读：{exc}") from exc
        return StoredHead(
            size=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        )

    def open_read(self, key: str, *, start: int | None = None, end: int | None = None) -> OpenedStream:
        path = self._path(key)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise StorageNotFound("对象不存在") from exc
        except OSError as exc:
            raise StorageUnavailable(f"本地存储不可读：{exc}") from exc

        offset = 0 if start is None else max(int(start), 0)
        try:
            handle.seek(offset)
        except OSError as exc:
            handle.close()
            raise StorageUnavailable(f"本地存储定位失败：{exc}") from exc

        remaining = None if start is None or end is None else max(int(end) - offset + 1, 0)
        chunks = _iter_file(handle, remaining)
        return OpenedStream(chunks=chunks)

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageUnavailable(f"本地存储删除失败：{exc}") from exc

    def presigned_url(self, key: str, expires_seconds: int) -> str | None:
        # 本地后端没有「短时 URL」概念：调用方必须回落代理模式
        del key, expires_seconds
        return None

    def health(self) -> dict[str, object]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(f"本地存储目录不可创建：{exc}") from exc
        if not os.access(self.root, os.W_OK):
            raise StorageUnavailable("本地存储目录不可写")
        return {"backend": "local", "ok": True}

    def iter_objects(self, prefix: str, *, limit: int = 1000) -> Iterator[ObjectRef]:
        cleaned = prefix.strip("/")
        count = 0
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*")):
            if count >= limit:
                break
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if cleaned and not key.startswith(cleaned):
                continue
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - 并发删除
                continue
            yield ObjectRef(
                key=key,
                size=stat.st_size,
                last_modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            )
            count += 1


def _iter_file(handle, remaining: int | None) -> Iterator[bytes]:
    try:
        while True:
            wanted = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if wanted <= 0:
                break
            data = handle.read(wanted)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data
    finally:
        handle.close()
