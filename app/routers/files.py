"""文件接口：上传 / 列表 / 详情 / 内容 / 改可见性 / 删除 / 授权。

下载链路是本文件里最需要小心的部分：

* **代理模式**：`StreamingResponse` 逐块转发（local 与 cos 同一条代码路径）；
* **预签名模式**：302 到 COS 短时 URL（后端不支持时自动回落代理）；
* `ETag` = 带引号的 sha256；`If-None-Match` 命中先回 304（**优先于** 302）；
* `Range` 只支持单区间（多区间按 416 处理，不实现 multipart/byteranges）；
* `HEAD` 永远不 302、不读对象，只回头部。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from .. import files_service
from ..constants import STATUS_DELETED
from ..deps import (
    DbDep,
    ReadIdentity,
    SettingsDep,
    StoragesDep,
    WriteIdentity,
    check_write_rate_limit,
    client_ip,
)
from ..errors import api_error, storage_unavailable
from ..models import FileRecord
from ..schemas import GrantRequest, VisibilityPatch
from ..storage.base import StorageError, StorageNotFound, StorageUnavailable, content_disposition

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_CONTROL_PRIVATE = "private, max-age=0, must-revalidate"


# ------------------------------------------------------------------ 响应头工具


def _etag(record: FileRecord) -> str:
    return f'"{record.sha256}"'


def _base_headers(record: FileRecord, etag: str) -> dict[str, str]:
    return {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Cache-Control": _CACHE_CONTROL_PRIVATE,
        "Content-Disposition": content_disposition(record.filename),
        "Content-Type": record.content_type,
        # 纵深防御：即使类型判断出错，也不让浏览器内联执行/嵌入
        "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def _if_none_match(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in str(header).split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == "*" or value == etag:
            return True
    return False


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """只接受单区间：``bytes=start-end`` / ``bytes=start-`` / ``bytes=-suffix``。"""
    text = str(header or "").strip()
    if not text.lower().startswith("bytes="):
        raise _range_error(size)
    spec = text[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        raise _range_error(size)
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if first == "":
        if not last.isdigit():
            raise _range_error(size)
        suffix = int(last)
        if suffix <= 0 or size <= 0:
            raise _range_error(size)
        return max(size - suffix, 0), size - 1
    if not first.isdigit():
        raise _range_error(size)
    start = int(first)
    if last == "":
        end = size - 1
    else:
        if not last.isdigit():
            raise _range_error(size)
        end = int(last)
        if end < start:
            raise _range_error(size)
    if start >= size:
        raise _range_error(size)
    return start, min(end, size - 1)


def _range_error(size: int) -> Exception:
    return api_error(
        416,
        "range_not_satisfiable",
        "请求的字节范围不合法",
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def _grant_count(db: Any, record: FileRecord) -> int:
    return files_service.grant_counts(db, [record.id]).get(record.id, 0)


# --------------------------------------------------------------------- 上传


@router.post("/v1/files", status_code=201)
def upload_file(
    request: Request,
    identity: WriteIdentity,
    settings: SettingsDep,
    db: DbDep,
    storages: StoragesDep,
    file: UploadFile = File(..., description="要保存的文件"),
    ttl_seconds: str | None = Form(
        None,
        description="有效期（秒）；0 = 永久，仅允许与 visibility=public 一起使用",
    ),
    visibility: str | None = Form(None, description="public / private / protected；缺省 private"),
) -> dict[str, Any]:
    ip = client_ip(request)
    check_write_rate_limit(settings=settings, user_id=identity.sub, ip=ip)
    try:
        record = files_service.save_upload(
            db,
            settings,
            storages,
            user_id=identity.sub,
            upload=file,
            ttl_seconds=ttl_seconds,
            visibility=visibility,
            request_ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except StorageUnavailable as exc:
        logger.warning("上传写存储失败: sub=%s err=%s", identity.sub, exc)
        raise storage_unavailable() from exc
    except StorageError as exc:  # pragma: no cover - 防御性
        logger.warning("上传写存储失败: sub=%s err=%s", identity.sub, exc)
        raise storage_unavailable() from exc
    return record.to_public_dict()


# --------------------------------------------------------------------- 列表


@router.get("/v1/files")
def list_files(
    identity: ReadIdentity,
    settings: SettingsDep,
    db: DbDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    status: str = Query("active", description="active / expired / deleted / all"),
) -> dict[str, Any]:
    rows, total = files_service.list_files(
        db, settings, sub=identity.sub, limit=limit, offset=offset, status=status
    )
    counts = files_service.grant_counts(db, [row.id for row in rows])
    return {
        "files": [row.to_public_dict(grant_count=counts.get(row.id, 0)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "quota": files_service.quota_snapshot(db, settings, sub=identity.sub),
    }


@router.get("/v1/files/{file_id}")
def get_file(
    request: Request,
    file_id: str,
    identity: ReadIdentity,
    db: DbDep,
) -> dict[str, Any]:
    record = files_service.load_for_read(
        db, file_id=file_id, sub=identity.sub, request_ip=client_ip(request)
    )
    return record.to_public_dict(grant_count=_grant_count(db, record))


# --------------------------------------------------------------------- 内容


@router.api_route("/v1/files/{file_id}/content", methods=["GET", "HEAD"])
def download_content(
    request: Request,
    file_id: str,
    identity: ReadIdentity,
    settings: SettingsDep,
    db: DbDep,
    storages: StoragesDep,
    direct: str | None = Query(
        None,
        description="1 = 要求 302 到预签名 URL，0 = 强制代理（受 DOWNLOAD_MODE 约束）",
    ),
) -> Response:
    ip = client_ip(request)
    record = files_service.load_for_read(db, file_id=file_id, sub=identity.sub, request_ip=ip)
    etag = _etag(record)
    headers = _base_headers(record, etag)

    if _if_none_match(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": _CACHE_CONTROL_PRIVATE,
                "X-Content-Type-Options": "nosniff",
            },
        )

    backend = storages.for_record(record)
    mode = files_service.resolve_download_mode(settings, direct)
    if mode == "presign" and request.method != "HEAD":
        try:
            url = backend.presigned_url(record.storage_key, settings.presign_expires_seconds)
        except StorageError as exc:
            logger.warning("预签名失败: file=%s err=%s", record.id, exc)
            raise storage_unavailable() from exc
        if url:
            headers.update(
                {
                    "Location": url,
                    "Cache-Control": "no-store",
                    "X-File-Download-Mode": "presign",
                }
            )
            return Response(status_code=302, headers=headers)
        # local 后端没有预签名能力：静默回落代理（启动时已打 WARNING）
        logger.debug("后端不支持预签名，回落代理: backend=%s", backend.name)

    if request.method == "HEAD":
        headers["Content-Length"] = str(record.size_bytes)
        return Response(status_code=200, headers=headers)

    range_header = request.headers.get("range")
    if range_header:
        if_range = request.headers.get("if-range")
        if if_range and if_range.strip() not in (etag, etag.strip('"')):
            # If-Range 不匹配 → 忽略 Range，回整份内容
            range_header = None

    start: int | None = None
    end: int | None = None
    if range_header:
        start, end = _parse_range(range_header, record.size_bytes)
        headers["Content-Range"] = f"bytes {start}-{end}/{record.size_bytes}"
        headers["Content-Length"] = str(end - start + 1)
    else:
        headers["Content-Length"] = str(record.size_bytes)

    try:
        opened = backend.open_read(record.storage_key, start=start, end=end)
    except StorageNotFound as exc:
        files_service.mark_missing_object(db, record, request_ip=ip)
        raise api_error(404, "file_not_found", "文件不存在或无权访问") from exc
    except StorageUnavailable as exc:
        logger.warning("读取对象失败: file=%s err=%s", record.id, exc)
        raise storage_unavailable() from exc
    except StorageError as exc:  # pragma: no cover - 防御性
        logger.warning("读取对象失败: file=%s err=%s", record.id, exc)
        raise storage_unavailable() from exc

    return StreamingResponse(
        opened.chunks,
        status_code=206 if start is not None else 200,
        headers=headers,
    )


# --------------------------------------------------------------- 改可见性


@router.patch("/v1/files/{file_id}")
def patch_file(
    request: Request,
    file_id: str,
    payload: VisibilityPatch,
    identity: WriteIdentity,
    settings: SettingsDep,
    db: DbDep,
) -> dict[str, Any]:
    ip = client_ip(request)
    check_write_rate_limit(settings=settings, user_id=identity.sub, ip=ip)
    record, _changed = files_service.set_visibility(
        db,
        settings,
        file_id=file_id,
        sub=identity.sub,
        visibility=payload.visibility,
        request_ip=ip,
    )
    return record.to_public_dict(grant_count=_grant_count(db, record))


# --------------------------------------------------------------------- 删除


@router.delete("/v1/files/{file_id}")
def delete_file(
    request: Request,
    file_id: str,
    identity: WriteIdentity,
    settings: SettingsDep,
    db: DbDep,
    storages: StoragesDep,
) -> dict[str, Any]:
    ip = client_ip(request)
    check_write_rate_limit(settings=settings, user_id=identity.sub, ip=ip)
    record, changed = files_service.delete_file(
        db, storages, file_id=file_id, sub=identity.sub, request_ip=ip
    )
    return {
        "id": record.id,
        "status": STATUS_DELETED,
        "changed": changed,
    }


# --------------------------------------------------------------------- 授权


@router.get("/v1/files/{file_id}/grants")
def get_grants(file_id: str, identity: WriteIdentity, db: DbDep) -> dict[str, Any]:
    record, grants = files_service.list_grants(db, file_id=file_id, sub=identity.sub)
    return {
        "file_id": record.id,
        "visibility": record.visibility,
        "grants": [grant.to_public_dict() for grant in grants],
    }


@router.post("/v1/files/{file_id}/grants", status_code=201)
def create_grant(
    request: Request,
    file_id: str,
    payload: GrantRequest,
    identity: WriteIdentity,
    settings: SettingsDep,
    db: DbDep,
    response: Response,
) -> dict[str, Any]:
    ip = client_ip(request)
    check_write_rate_limit(settings=settings, user_id=identity.sub, ip=ip)
    grant, created = files_service.add_grant(
        db,
        settings,
        file_id=file_id,
        sub=identity.sub,
        grantee_sub=payload.grantee_sub,
        request_ip=ip,
    )
    if not created:
        response.status_code = 200
    return {
        "file_id": grant.file_id,
        "grantee_sub": grant.grantee_sub,
        "created_at": grant.created_at,
        "created": created,
    }


@router.delete("/v1/files/{file_id}/grants/{grantee_sub}")
def delete_grant(
    request: Request,
    file_id: str,
    grantee_sub: str,
    identity: WriteIdentity,
    settings: SettingsDep,
    db: DbDep,
) -> dict[str, Any]:
    ip = client_ip(request)
    check_write_rate_limit(settings=settings, user_id=identity.sub, ip=ip)
    record, removed = files_service.remove_grant(
        db, file_id=file_id, sub=identity.sub, grantee_sub=grantee_sub, request_ip=ip
    )
    return {"file_id": record.id, "grantee_sub": grantee_sub, "removed": removed}


# --------------------------------------------------------------------- 身份


@router.get("/v1/me")
def me(identity: ReadIdentity, settings: SettingsDep) -> dict[str, Any]:
    return {
        "sub": identity.sub,
        "username": identity.username,
        "scope": identity.scope,
        "issuer": settings.sso_issuer,
        "audience": settings.jwt_audience,
    }
