"""腾讯云 COS 后端（生产）。

为什么用 SDK 而不是挂载：

* 服务器是 snap 装的 Docker，**容器内看不到宿主的 `/elicos` cosfs 挂载**（实测容器里是
  空目录、inode 与宿主不同）；在容器里自挂 cosfs 又要 ``SYS_ADMIN`` + FUSE + 把凭据塞进容器。
* 桶 ``elicoud-cos-1465229333``（``/elicos`` 那个）**匿名可列举**，只能放公开内容；
  本服务用私有桶 ``elicoudcos-1465229333``（ap-nanjing，150 GiB）。

对象约定：

* key 形如 ``<COS_KEY_PREFIX>/blobs/<id 第 3-4 位>/<id>``，由文件 id 派生；
* 每个对象显式 ``ACL=private``（纵深防御：即使桶策略被改错也不至于匿名可读）；
* 对象头带 ``Content-Type`` / ``Content-Disposition`` / ``x-cos-meta-sha256``，
  这样预签名直链也能给出正确的下载头。

错误语义（关键）：

* ``NoSuchKey`` / 404 → :class:`StorageNotFound`（对调用方 404）
* 403 / 5xx / 超时 / 网络错误 → :class:`StorageUnavailable`（对调用方 503）
  403 **绝不能**当成「文件不存在」：那是桶名/密钥/策略配错了，必须显式失败。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, BinaryIO

from ..constants import CHUNK_SIZE
from .base import (
    ObjectRef,
    OpenedStream,
    StorageNotFound,
    StorageUnavailable,
    StoredHead,
    content_disposition,
    validate_file_id,
    validate_key,
)

logger = logging.getLogger(__name__)

HEALTH_CACHE_SECONDS = 15.0
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})


def _status_of(exc: BaseException) -> int | None:
    getter = getattr(exc, "get_status_code", None)
    if callable(getter):
        try:
            value = getter()
            return int(value) if value is not None else None
        except (TypeError, ValueError):  # pragma: no cover - 防御性
            return None
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover
        return None


def _code_of(exc: BaseException) -> str:
    getter = getattr(exc, "get_error_code", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - 纯诊断用途
            return ""
    return str(getattr(exc, "error_code", "") or "")


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class CosBlobStore:
    name = "cos"

    def __init__(self, settings, client: Any | None = None) -> None:
        self.settings = settings
        self.bucket = settings.cos_bucket
        self.prefix = settings.cos_key_prefix
        self.objects_prefix = f"{self.prefix}/blobs"
        self._client = client
        self._client_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._health_ok: bool | None = None
        self._health_detail: dict[str, object] = {}
        self._health_checked_at = 0.0

    # ------------------------------------------------------------------ 客户端
    @property
    def client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
            return self._client

    def _build_client(self) -> Any:
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:  # pragma: no cover - 依赖在 requirements 里
            raise StorageUnavailable("未安装 cos-python-sdk-v5，无法使用 COS 后端") from exc

        kwargs: dict[str, object] = {
            "Region": self.settings.cos_region,
            "SecretId": self.settings.cos_secret_id,
            "SecretKey": self.settings.cos_secret_key,
            "Scheme": self.settings.cos_scheme,
            "Timeout": float(self.settings.cos_timeout_seconds),
        }
        # 留空时由 SDK 走默认域名 cos.<region>.myqcloud.com（同地域内网，免流量费）
        if self.settings.cos_endpoint:
            kwargs["Endpoint"] = self.settings.cos_endpoint
        try:
            config = CosConfig(**kwargs)
            return CosS3Client(config, retry=int(self.settings.cos_max_retries))
        except Exception as exc:  # noqa: BLE001 - 配置错误也要变成 503 而不是 500 堆栈
            logger.error("初始化 COS 客户端失败：%s", exc)
            raise StorageUnavailable("初始化 COS 客户端失败，请检查 COS_* 配置") from exc

    # ------------------------------------------------------------------ 内部
    def _translate(self, exc: BaseException, *, op: str, allow_not_found: bool = False) -> Exception:
        status = _status_of(exc)
        code = _code_of(exc)
        if allow_not_found and (status == 404 or code in _NOT_FOUND_CODES):
            return StorageNotFound("对象不存在")
        # 只记操作名与状态码：不记对象 key（含文件 id）、不记任何凭据
        logger.warning("COS %s 失败 status=%s code=%s", op, status, code or "-")
        return StorageUnavailable(f"COS {op} 失败（status={status or '?'}, code={code or '?'}）")

    # ------------------------------------------------------------- StorageBackend
    def key_for(self, file_id: str) -> str:
        identifier = validate_file_id(file_id)
        return f"{self.prefix}/blobs/{identifier[2:4]}/{identifier}"

    def write(
        self,
        source: BinaryIO,
        key: str,
        *,
        content_type: str,
        filename: str,
        sha256: str,
    ) -> None:
        cleaned = validate_key(key)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=cleaned,
                Body=source,
                ContentType=content_type,
                ContentDisposition=content_disposition(filename),
                # SDK 的 Metadata 是「原样转发」的：自定义元数据要自己带 x-cos-meta- 前缀
                Metadata={"x-cos-meta-sha256": sha256},
                ACL="private",
            )
        except Exception as exc:  # noqa: BLE001 - 统一映射成 503
            raise self._translate(exc, op="put") from exc

    def head(self, key: str) -> StoredHead | None:
        cleaned = validate_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=cleaned)
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) == 404 or _code_of(exc) in _NOT_FOUND_CODES:
                return None
            raise self._translate(exc, op="head") from exc
        try:
            size = int(response.get("Content-Length") or 0)
        except (TypeError, ValueError):  # pragma: no cover
            size = 0
        return StoredHead(
            size=size,
            content_type=response.get("Content-Type"),
            etag=response.get("ETag"),
            last_modified=_parse_iso(response.get("Last-Modified")),
        )

    def open_read(self, key: str, *, start: int | None = None, end: int | None = None) -> OpenedStream:
        cleaned = validate_key(key)
        kwargs: dict[str, object] = {"Bucket": self.bucket, "Key": cleaned}
        if start is not None:
            last = "" if end is None else str(int(end))
            kwargs["Range"] = f"bytes={int(start)}-{last}"
        try:
            response = self.client.get_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, op="get", allow_not_found=True) from exc

        body = response.get("Body")
        stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
        if stream is None:  # pragma: no cover - 防御性
            raise StorageUnavailable("COS 返回了空的内容流")
        return OpenedStream(chunks=_iter_cos_stream(stream, body), size=_content_length(response))

    def delete(self, key: str) -> None:
        cleaned = validate_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=cleaned)
        except Exception as exc:  # noqa: BLE001
            # 对象本来就不存在时也视为成功（幂等删除）
            if _status_of(exc) == 404 or _code_of(exc) in _NOT_FOUND_CODES:
                return
            raise self._translate(exc, op="delete") from exc

    def presigned_url(self, key: str, expires_seconds: int) -> str | None:
        cleaned = validate_key(key)
        try:
            return str(
                self.client.get_presigned_url(
                    Bucket=self.bucket,
                    Key=cleaned,
                    Method="GET",
                    Expired=int(expires_seconds),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("COS 预签名失败：%s", type(exc).__name__)
            raise StorageUnavailable("生成预签名下载 URL 失败") from exc

    def health(self) -> dict[str, object]:
        now = time.monotonic()
        with self._health_lock:
            if self._health_checked_at and now - self._health_checked_at < HEALTH_CACHE_SECONDS:
                if not self._health_ok:
                    raise StorageUnavailable("COS 探活失败（15s 内缓存）")
                return dict(self._health_detail)
            try:
                client = self.client
                try:
                    client.head_bucket(Bucket=self.bucket)
                except AttributeError:  # 老版本 SDK 没有 head_bucket
                    client.list_objects(Bucket=self.bucket, MaxKeys=1)
            except StorageUnavailable:
                self._health_ok = False
                self._health_detail = {}
                self._health_checked_at = now
                raise
            except Exception as exc:  # noqa: BLE001
                self._health_ok = False
                self._health_detail = {"error": type(exc).__name__}
                self._health_checked_at = now
                logger.warning("COS 探活失败：%s", type(exc).__name__)
                raise StorageUnavailable("COS 不可达或凭据/桶名配置有误") from exc

            self._health_ok = True
            self._health_detail = {
                "backend": "cos",
                "bucket": self.bucket,
                "region": self.settings.cos_region,
                "ok": True,
            }
            self._health_checked_at = now
            return dict(self._health_detail)

    def iter_objects(self, prefix: str, *, limit: int = 1000) -> Iterator[ObjectRef]:
        base = validate_key(prefix) if prefix.strip("/") else ""
        marker = ""
        emitted = 0
        while emitted < limit:
            try:
                response = self.client.list_objects(
                    Bucket=self.bucket,
                    Prefix=base,
                    Marker=marker,
                    MaxKeys=min(1000, limit - emitted),
                )
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc, op="list") from exc
            contents = response.get("Contents") or []
            for item in contents:
                yield ObjectRef(
                    key=str(item.get("Key")),
                    size=int(item.get("Size") or 0),
                    last_modified=_parse_iso(item.get("LastModified")),
                )
                emitted += 1
            if str(response.get("IsTruncated", "false")).lower() != "true" or not contents:
                break
            marker = str(response.get("NextMarker") or contents[-1].get("Key"))


def _content_length(response: dict[str, Any]) -> int | None:
    try:
        return int(response.get("Content-Length") or 0)
    except (TypeError, ValueError):  # pragma: no cover
        return None


def _iter_cos_stream(stream: Any, body: Any) -> Iterator[bytes]:
    try:
        while True:
            data = stream.read(CHUNK_SIZE)
            if not data:
                break
            yield data
    finally:
        for target in (stream, body):
            closer = getattr(target, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 关流失败不该影响已经发完的响应
                    logger.debug("关闭 COS 内容流失败", exc_info=True)
