"""存储后端工厂与「按行选后端」的路由器。

* **写入**用配置的当前后端（``settings.storage_backend``）；
* **读取**按 ``files.storage_backend`` 选后端 —— 换后端后旧文件还能读，
  而不是「一改配置，历史文件全 404」。
"""

from __future__ import annotations

import threading

from ..config import Settings
from .base import ObjectRef, OpenedStream, StorageBackend, StorageError, StorageNotFound, StorageUnavailable, StoredHead


def build_storage(settings: Settings, *, backend: str | None = None) -> StorageBackend:
    name = str(backend or settings.storage_backend).strip().lower()
    if name == "local":
        from .local import LocalBlobStore

        return LocalBlobStore(settings)
    if name == "cos":
        from .cos import CosBlobStore

        return CosBlobStore(settings)
    raise StorageUnavailable(f"未知的存储后端：{name}")


class StorageRegistry:
    """惰性构造并缓存后端实例；测试可以注入替身（``primary``）。"""

    def __init__(self, settings: Settings, *, primary: StorageBackend | None = None) -> None:
        self.settings = settings
        self.primary = primary or build_storage(settings)
        self._backends: dict[str, StorageBackend] = {self.primary.name: self.primary}
        self._lock = threading.Lock()

    def for_name(self, name: str | None) -> StorageBackend:
        key = str(name or self.primary.name).strip().lower()
        with self._lock:
            backend = self._backends.get(key)
            if backend is None:
                backend = build_storage(self.settings, backend=key)
                self._backends[key] = backend
            return backend

    def for_record(self, record: object) -> StorageBackend:
        return self.for_name(getattr(record, "storage_backend", None))

    @property
    def known(self) -> dict[str, StorageBackend]:
        return dict(self._backends)


__all__ = [
    "ObjectRef",
    "OpenedStream",
    "StorageBackend",
    "StorageError",
    "StorageNotFound",
    "StorageRegistry",
    "StorageUnavailable",
    "StoredHead",
    "build_storage",
]
