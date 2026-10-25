"""存储抽象：业务层只依赖这里的协议，不依赖「本地磁盘」或「COS SDK」。

为什么要有这层：

* 生产内容在**私有 COS 桶**（`elicoudcos-1465229333`），本机只有元数据与上传暂存；
* CI / 本地开发没有 COS 凭据，用 ``local`` 后端跑同一套测试；
* 两种后端的行为必须一致（分片读、404 语义、删除幂等、健康检查），
  否则「本地测通了、线上 404」这种问题只会在部署后发现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Iterator, Protocol, runtime_checkable
from urllib.parse import quote

from ..constants import FILE_ID_RE

_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_FALLBACK = re.compile(r'[^ -~]|["\\]')


class StorageError(RuntimeError):
    """存储层的基类异常（业务层只处理它和下面两个子类）。"""


class StorageUnavailable(StorageError):
    """后端不可达 / 凭据错误 / 超时 —— 对调用方是 **503**，绝不当作「文件不存在」。"""


class StorageNotFound(StorageError):
    """对象确实不在（NoSuchKey）—— 对调用方是 **404**。"""


@dataclass(frozen=True)
class StoredHead:
    size: int
    content_type: str | None = None
    etag: str | None = None
    last_modified: datetime | None = None


@dataclass(frozen=True)
class ObjectRef:
    """列举桶时的轻量条目（孤儿清理用）。"""

    key: str
    size: int = 0
    last_modified: datetime | None = None


@dataclass
class OpenedStream:
    """已经打开好的对象内容流。

    ``chunks`` 必须是「已经被后端建立」的迭代器：建立连接/COS 请求发生在
    ``open_read()`` 调用时，这样 404/503 能在把响应头发出去**之前**映射成正确状态码，
    而不是「200 已发出、随后流里抛异常」。
    """

    chunks: Iterator[bytes]
    size: int | None = None


@runtime_checkable
class StorageBackend(Protocol):
    """内容存储后端。所有 key 都是 posix 风格字符串，由后端自己生成。"""

    name: str

    def key_for(self, file_id: str) -> str:
        """由文件 id 派生对象 key（不接受任何用户输入拼接）。"""

    def write(
        self,
        source: BinaryIO,
        key: str,
        *,
        content_type: str,
        filename: str,
        sha256: str,
    ) -> None:
        """把 ``source`` 的全部内容写到 ``key``；失败抛 StorageUnavailable。"""

    def head(self, key: str) -> StoredHead | None:
        """对象不存在返回 None；后端故障抛 StorageUnavailable。"""

    def open_read(self, key: str, *, start: int | None = None, end: int | None = None) -> OpenedStream:
        """打开对象（可选闭区间字节范围）；不存在抛 StorageNotFound。"""

    def delete(self, key: str) -> None:
        """删除对象；对象本来就不存在也算成功（幂等）。"""

    def presigned_url(self, key: str, expires_seconds: int) -> str | None:
        """返回可直接下载的短时 URL；后端不支持时返回 None（调用方回落代理）。"""

    def health(self) -> dict[str, object]:
        """探活；失败抛 StorageUnavailable。实现应有缓存，避免探针打爆后端。"""

    def iter_objects(self, prefix: str, *, limit: int = 1000) -> Iterator[ObjectRef]:
        """列举对象（仅供 CLI 的孤儿清理，正常请求路径**绝不**列举桶）。"""


# --------------------------------------------------------------------- 工具


def validate_file_id(file_id: str) -> str:
    if not FILE_ID_RE.match(str(file_id or "")):
        raise StorageError("非法的文件 id，拒绝构造对象 key")
    return str(file_id)


def validate_key(key: str) -> str:
    """对象 key 必须是我们生成的相对路径；绝不接受 `..` 之类的东西。"""
    cleaned = str(key or "").strip("/")
    if not cleaned or not _KEY_RE.match(cleaned) or ".." in cleaned.split("/"):
        raise StorageError("非法的对象 key")
    return cleaned


def content_disposition(filename: str) -> str:
    """``attachment; filename="ascii"; filename*=UTF-8''...``

    ASCII 版本只用于老客户端；真正的原始文件名走 RFC 5987 的 ``filename*``。
    """
    fallback = "".join("_" if _SAFE_FALLBACK.search(ch) else ch for ch in filename)[:100]
    if not fallback.strip("_ "):
        fallback = "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
