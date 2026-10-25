"""文件服务业务层：校验、配额、保存、可见性判定、授权、清理、审计。

HTTP 层（`app/routers/files.py`）只负责解析请求与拼响应；所有「谁能看什么」的判断
都集中在 :func:`can_read` / :func:`load_for_read` / :func:`load_for_owner`，
避免某条路由漏判可见性 —— 这是本服务最要紧的一条安全边界。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .constants import (
    ACTION_DELETE,
    ACTION_DENY,
    ACTION_EXPIRE,
    ACTION_GRANT_ADD,
    ACTION_GRANT_REMOVE,
    ACTION_MISSING_OBJECT,
    ACTION_UPLOAD,
    ACTION_VISIBILITY,
    CHUNK_SIZE,
    DANGEROUS_CONTENT_TYPES,
    FILE_ID_RE,
    GRANTEE_SUB_RE,
    PERMANENT_EXPIRES_AT,
    PERMANENT_TTL_SECONDS,
    RESULT_DENIED,
    RESULT_IDEMPOTENT,
    RESULT_NOT_FOUND,
    RESULT_OK,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_EXPIRED,
    VISIBILITIES,
    VISIBILITY_PROTECTED,
    VISIBILITY_PUBLIC,
)
from .errors import api_error, limiter, storage_unavailable
from .models import AuditLog, FileGrant, FileRecord, dumps_detail, to_iso, utcnow
from .storage import StorageRegistry
from .storage.base import StorageError, StorageUnavailable

logger = logging.getLogger(__name__)

# 拒绝审计的写入限流：坏令牌/乱猜 id 刷审计表的成本必须可控
DENY_AUDIT_ATTEMPTS = 60
DENY_AUDIT_WINDOW_SECONDS = 300

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|]')
_PATH_SPLIT_RE = re.compile(r"[\\/]")
_CONTENT_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$")


# --------------------------------------------------------------------- 输入


def new_file_id() -> str:
    """``f_`` + 22 个 URL-safe 字符（96 bit 随机；不可猜测）。"""
    return f"f_{secrets.token_urlsafe(16)}"


def sanitize_filename(name: str | None, *, fallback: str = "file") -> str:
    """去掉路径、控制字符与文件系统保留字符；长度上限 200。

    * 只保留最后一段：``../../etc/passwd`` → ``passwd``
    * 去掉 CR/LF（防响应头注入）与控制字符
    * 去掉开头/结尾的点（防隐藏文件与 ``..``）
    """
    candidate = _PATH_SPLIT_RE.split(str(name or ""))[-1]
    candidate = _CONTROL_RE.sub("", candidate)
    candidate = _FORBIDDEN_RE.sub("_", candidate)
    candidate = candidate.strip().strip(".").strip()
    if len(candidate) > 200:
        candidate = candidate[:200]
    return candidate or fallback


def normalize_content_type(value: str | None) -> str:
    """取 ``type/subtype``（丢弃参数），非法则 400。缺省按 ``application/octet-stream``。"""
    raw = str(value or "").split(";")[0].strip().lower()
    if not raw:
        return "application/octet-stream"
    if not _CONTENT_TYPE_RE.match(raw):
        raise api_error(400, "invalid_request", "Content-Type 不合法（只接受 type/subtype）")
    return raw


def effective_content_type(content_type: str) -> str:
    """危险类型降级：预签名直链无法追加 CSP/nosniff，只能不让浏览器按可执行类型解析。"""
    return "application/octet-stream" if content_type in DANGEROUS_CONTENT_TYPES else content_type


def resolve_ttl(settings: Settings, raw: Any) -> int:
    if raw is None or str(raw).strip() == "":
        return settings.default_ttl_seconds
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise api_error(400, "invalid_request", "ttl_seconds 必须是整数秒") from exc
    if value == PERMANENT_TTL_SECONDS:
        return value
    if value < 1 or value > settings.max_ttl_seconds:
        raise api_error(
            400,
            "invalid_request",
            f"ttl_seconds 必须是 0（永久，仅 public）或 1..{settings.max_ttl_seconds}"
            f"（最多 {settings.max_ttl_seconds // 86400} 天）",
        )
    return value


def resolve_visibility(settings: Settings, raw: Any) -> str:
    if raw is None or str(raw).strip() == "":
        return settings.default_visibility
    value = str(raw).strip().lower()
    if value not in VISIBILITIES:
        raise api_error(400, "invalid_request", "visibility 只允许 public / private / protected")
    return value


def resolve_download_mode(settings: Settings, direct: str | None) -> str:
    """``DOWNLOAD_MODE`` × ``?direct=`` → 实际模式（``proxy`` / ``presign``）。"""
    flag = str(direct or "").strip().lower()
    wants_direct = flag in ("1", "true", "yes", "on")
    wants_proxy = flag in ("0", "false", "no", "off")
    mode = settings.download_mode
    if mode == "proxy":
        return "proxy"
    if mode == "presign":
        return "proxy" if wants_proxy else "presign"
    # both：默认代理，显式 ?direct=1 才 302
    return "presign" if wants_direct else "proxy"


# --------------------------------------------------------------------- 审计


def audit(
    session: Session,
    *,
    user_id: str | None,
    action: str,
    target: str | None = None,
    result: str = RESULT_OK,
    detail: dict[str, Any] | None = None,
    request_ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            id=f"al_{secrets.token_hex(10)}",
            user_id=user_id,
            action=action,
            target=target,
            result=result,
            detail=dumps_detail(detail),
            request_ip=request_ip,
            created_at=to_iso(utcnow()),
        )
    )


def _audit_denied(
    session: Session,
    *,
    user_id: str | None,
    target: str | None,
    detail: dict[str, Any],
    request_ip: str | None,
) -> None:
    """被拒绝的读取也留痕；按 IP 限流，且审计失败绝不掩盖原来的 404/410。"""
    if request_ip and not limiter.allow(f"file-deny:{request_ip}", DENY_AUDIT_ATTEMPTS, DENY_AUDIT_WINDOW_SECONDS):
        return
    try:
        audit(
            session,
            user_id=user_id,
            action=ACTION_DENY,
            target=target,
            result=RESULT_DENIED,
            detail=detail,
            request_ip=request_ip,
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能把 404 变成 500
        session.rollback()
        logger.exception("写拒绝审计失败")


# ----------------------------------------------------------------- 可见性判定


def can_read(session: Session, record: FileRecord, sub: str) -> bool:
    """可见性矩阵的唯一实现（§3.2）：

    | visibility | 属主 | 被授权 sub | 其他已登录用户 |
    | private    | ✅   | ❌         | ❌             |
    | protected  | ✅   | ✅         | ❌             |
    | public     | ✅   | ✅         | ✅             |
    """
    if record.user_id == sub:
        return True
    if record.visibility == VISIBILITY_PUBLIC:
        return True
    if record.visibility == VISIBILITY_PROTECTED:
        return session.get(FileGrant, (record.id, sub)) is not None
    return False


def _not_found() -> Exception:
    # 不存在 / 无权 / 已删除 一律 404，且文案完全相同：不泄露文件是否存在
    return api_error(404, "file_not_found", "文件不存在或无权访问")


def _expired() -> Exception:
    return api_error(410, "file_expired", "文件已过期")


def get_record(session: Session, file_id: str) -> FileRecord | None:
    text = str(file_id or "")
    if not FILE_ID_RE.match(text):
        return None
    return session.get(FileRecord, text)


def load_for_read(
    session: Session,
    *,
    file_id: str,
    sub: str,
    request_ip: str | None = None,
) -> FileRecord:
    record = get_record(session, file_id)
    if record is None or not can_read(session, record, sub):
        _audit_denied(
            session,
            user_id=sub,
            target=str(file_id)[:64] if FILE_ID_RE.match(str(file_id or "")) else None,
            detail={"reason": "not_found_or_forbidden"},
            request_ip=request_ip,
        )
        raise _not_found()

    status = record.effective_status()
    if status == STATUS_DELETED:
        raise _not_found()
    if status == STATUS_EXPIRED:
        raise _expired()
    return record


def load_for_owner(session: Session, *, file_id: str, sub: str) -> FileRecord:
    """属主操作（PATCH / DELETE / grants）：非属主一律 404。"""
    record = get_record(session, file_id)
    if record is None or record.user_id != sub:
        raise _not_found()
    status = record.effective_status()
    if status == STATUS_DELETED:
        raise _not_found()
    if status == STATUS_EXPIRED:
        raise _expired()
    return record


# --------------------------------------------------------------------- 配额


def quota_snapshot(session: Session, settings: Settings, *, sub: str) -> dict[str, Any]:
    """每用户配额：只把「仍然有效」的文件算进去（过期文件不该占住用户的名额）。"""
    now_iso = to_iso(utcnow())
    files, used = session.execute(
        select(func.count(FileRecord.id), func.coalesce(func.sum(FileRecord.size_bytes), 0)).where(
            FileRecord.user_id == sub,
            FileRecord.status == STATUS_ACTIVE,
            FileRecord.expires_at > now_iso,
        )
    ).one()
    files = int(files or 0)
    used = int(used or 0)
    return {
        "files": files,
        "bytes": used,
        "max_files": settings.quota_max_files,
        "max_bytes": settings.quota_max_bytes,
        "files_percent": round(files * 100 / settings.quota_max_files, 2),
        "bytes_percent": round(used * 100 / settings.quota_max_bytes, 2),
    }


def total_active_bytes(session: Session) -> int:
    """全局已用容量：按 DB 汇总（桶是专用的，DB 是权威真源）。

    ``active`` 包含「已过期但清理器还没扫到」的行：在对象真正删除之前，它们确实还占着桶。
    """
    value = session.scalar(
        select(func.coalesce(func.sum(FileRecord.size_bytes), 0)).where(FileRecord.status == STATUS_ACTIVE)
    )
    return int(value or 0)


def global_quota_snapshot(session: Session, settings: Settings) -> dict[str, Any]:
    used = total_active_bytes(session)
    percent = round(used * 100 / settings.max_total_bytes, 2) if settings.max_total_bytes else 100.0
    payload: dict[str, Any] = {
        "used_bytes": used,
        "limit_bytes": settings.max_total_bytes,
        "percent": percent,
        "warning": percent >= settings.storage_warn_percent,
    }
    expires = settings.capacity_expires_date
    if expires is not None:
        remaining = (expires - utcnow().date()).days
        payload["capacity_expires_at"] = expires.isoformat()
        payload["capacity_expires_in_days"] = remaining
        payload["capacity_expiring_soon"] = remaining <= 120
    return payload


def _check_temp_space(settings: Settings) -> Path:
    """上传暂存盘余量门槛：低于 MIN_FREE_BYTES 就拒绝，避免把宿主磁盘写满。"""
    root = Path(settings.temp_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(root)
    except OSError as exc:
        raise storage_unavailable("服务器暂存目录不可用，请稍后重试") from exc
    if usage.free < settings.min_free_bytes + settings.max_file_bytes:
        raise storage_unavailable("服务器暂存空间不足，请稍后重试")
    return root


# --------------------------------------------------------------------- 上传


def save_upload(
    session: Session,
    settings: Settings,
    storages: StorageRegistry,
    *,
    user_id: str,
    upload: UploadFile,
    ttl_seconds: Any = None,
    visibility: Any = None,
    request_ip: str | None = None,
    user_agent: str | None = None,
) -> FileRecord:
    """流式落盘 → 算 sha256 → 写存储后端 → 写库（失败即回滚并删对象）。"""
    now = utcnow()
    filename = sanitize_filename(upload.filename)
    content_type = effective_content_type(normalize_content_type(upload.content_type))
    ttl = resolve_ttl(settings, ttl_seconds)
    vis = resolve_visibility(settings, visibility)
    if ttl == PERMANENT_TTL_SECONDS and vis != VISIBILITY_PUBLIC:
        raise api_error(
            400,
            "invalid_request",
            "永久文件必须同时设置 visibility=public",
        )

    # 1) 数量/字节配额（写入前先拦一次，避免白读一遍请求体）
    quota = quota_snapshot(session, settings, sub=user_id)
    if quota["files"] >= settings.quota_max_files:
        raise api_error(409, "quota_exceeded", f"文件数量已达上限（{settings.quota_max_files} 个）")
    if quota["bytes"] >= settings.quota_max_bytes:
        raise api_error(409, "quota_exceeded", "文件总容量已达上限，请先删除旧文件")

    if total_active_bytes(session) >= settings.max_total_bytes:
        raise api_error(409, "quota_exceeded", "服务总容量已满，请稍后重试或联系管理员")

    temp_root = _check_temp_space(settings)
    temp_path = temp_root / f"tmp_{secrets.token_hex(16)}"
    storage = storages.primary

    digest = hashlib.sha256()
    size = 0
    file_id: str | None = None
    storage_key: str | None = None
    object_written = False
    try:
        with temp_path.open("wb") as handle:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise api_error(
                        413,
                        "file_too_large",
                        f"文件超过上限 {settings.max_file_bytes} 字节",
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        sha256 = digest.hexdigest()

        # 2) 精确配额（现在知道真实大小了）
        if quota["bytes"] + size > settings.quota_max_bytes:
            raise api_error(409, "quota_exceeded", "文件总容量超出配额，请先删除旧文件")
        if total_active_bytes(session) + size > settings.max_total_bytes:
            raise api_error(409, "quota_exceeded", "服务总容量不足，请稍后重试")

        # 3) 写存储后端（key 由 id 派生，不拼用户输入）
        file_id = new_file_id()
        storage_key = storage.key_for(file_id)
        with temp_path.open("rb") as source:
            storage.write(
                source,
                storage_key,
                content_type=content_type,
                filename=filename,
                sha256=sha256,
            )
        object_written = True

        # 4) 写库 + 审计（同一事务）
        record = FileRecord(
            id=file_id,
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            sha256=sha256,
            storage_backend=storage.name,
            storage_key=storage_key,
            visibility=vis,
            status=STATUS_ACTIVE,
            created_at=to_iso(now),
            expires_at=PERMANENT_EXPIRES_AT if ttl == PERMANENT_TTL_SECONDS else to_iso(now + timedelta(seconds=ttl)),
            request_ip=request_ip,
            user_agent=(user_agent or "")[:512] or None,
        )
        session.add(record)
        audit(
            session,
            user_id=user_id,
            action=ACTION_UPLOAD,
            target=file_id,
            result=RESULT_OK,
            detail={
                "size_bytes": size,
                "visibility": vis,
                "content_type": content_type,
                "ttl_seconds": ttl,
                "permanent": ttl == PERMANENT_TTL_SECONDS,
            },
            request_ip=request_ip,
        )
        session.commit()
        return record
    except BaseException:
        session.rollback()
        if object_written and storage_key:
            try:
                storage.delete(storage_key)
            except StorageError:  # pragma: no cover - 依赖故障
                logger.warning("回滚时删除对象失败，等待孤儿清理: key=%s", storage_key)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


# --------------------------------------------------------------------- 列表


def list_files(
    session: Session,
    settings: Settings,
    *,
    sub: str,
    limit: int = 50,
    offset: int = 0,
    status: str = STATUS_ACTIVE,
) -> tuple[list[FileRecord], int]:
    now_iso = to_iso(utcnow())
    conditions = [FileRecord.user_id == sub]
    if status == STATUS_ACTIVE:
        conditions += [FileRecord.status == STATUS_ACTIVE, FileRecord.expires_at > now_iso]
    elif status == STATUS_EXPIRED:
        conditions.append(
            or_(
                FileRecord.status == STATUS_EXPIRED,
                and_(FileRecord.status == STATUS_ACTIVE, FileRecord.expires_at <= now_iso),
            )
        )
    elif status == STATUS_DELETED:
        conditions.append(FileRecord.status == STATUS_DELETED)
    elif status != "all":
        raise api_error(400, "invalid_request", "status 只允许 active / expired / deleted / all")

    total = int(session.scalar(select(func.count(FileRecord.id)).where(*conditions)) or 0)
    rows = list(
        session.scalars(
            select(FileRecord)
            .where(*conditions)
            .order_by(FileRecord.created_at.desc(), FileRecord.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return rows, total


def grant_counts(session: Session, file_ids: list[str]) -> dict[str, int]:
    if not file_ids:
        return {}
    rows = session.execute(
        select(FileGrant.file_id, func.count(FileGrant.grantee_sub))
        .where(FileGrant.file_id.in_(file_ids))
        .group_by(FileGrant.file_id)
    ).all()
    return {str(file_id): int(count) for file_id, count in rows}


# ----------------------------------------------------------------- 改可见性


def set_visibility(
    session: Session,
    settings: Settings,
    *,
    file_id: str,
    sub: str,
    visibility: Any,
    request_ip: str | None = None,
) -> tuple[FileRecord, bool]:
    record = load_for_owner(session, file_id=file_id, sub=sub)
    new_visibility = resolve_visibility(settings, visibility)
    old_visibility = record.visibility
    if old_visibility == new_visibility:
        return record, False
    record.visibility = new_visibility
    audit(
        session,
        user_id=sub,
        action=ACTION_VISIBILITY,
        target=record.id,
        result=RESULT_OK,
        detail={"from": old_visibility, "to": new_visibility},
        request_ip=request_ip,
    )
    session.commit()
    return record, True


# ------------------------------------------------------------------- 删除


def delete_file(
    session: Session,
    storages: StorageRegistry,
    *,
    file_id: str,
    sub: str,
    request_ip: str | None = None,
) -> tuple[FileRecord, bool]:
    """标记删除 → 提交 → 再删对象（对象删失败留待清理器/孤儿清理重试）。

    幂等：已删除的文件再删一次返回 ``changed=False``（**不是 404**）。
    """
    record = get_record(session, file_id)
    if record is None or record.user_id != sub:
        raise _not_found()

    status = record.effective_status()
    if status == STATUS_DELETED:
        return record, False
    if status == STATUS_EXPIRED:
        raise _expired()

    now_iso = to_iso(utcnow())
    record.status = STATUS_DELETED
    record.deleted_at = now_iso
    audit(
        session,
        user_id=sub,
        action=ACTION_DELETE,
        target=record.id,
        result=RESULT_OK,
        detail={"size_bytes": record.size_bytes},
        request_ip=request_ip,
    )
    session.commit()

    try:
        storages.for_record(record).delete(record.storage_key)
    except StorageError:
        logger.warning("删除对象失败，等待清理: file=%s", record.id)
    return record, True


# ------------------------------------------------------------------- 授权


def list_grants(session: Session, *, file_id: str, sub: str) -> tuple[FileRecord, list[FileGrant]]:
    record = load_for_owner(session, file_id=file_id, sub=sub)
    rows = list(
        session.scalars(
            select(FileGrant).where(FileGrant.file_id == record.id).order_by(FileGrant.created_at, FileGrant.grantee_sub)
        ).all()
    )
    return record, rows


def add_grant(
    session: Session,
    settings: Settings,
    *,
    file_id: str,
    sub: str,
    grantee_sub: str,
    request_ip: str | None = None,
) -> tuple[FileGrant, bool]:
    record = load_for_owner(session, file_id=file_id, sub=sub)
    grantee = str(grantee_sub or "").strip()
    if not GRANTEE_SUB_RE.match(grantee):
        raise api_error(400, "invalid_request", "grantee_sub 不合法（只允许 1-128 个 URL-safe 字符）")
    if grantee == sub:
        raise api_error(400, "invalid_request", "不能给自己授权（属主本来就能访问）")

    existing = session.get(FileGrant, (record.id, grantee))
    if existing is not None:
        return existing, False

    count = int(session.scalar(select(func.count(FileGrant.grantee_sub)).where(FileGrant.file_id == record.id)) or 0)
    if count >= settings.max_grants_per_file:
        raise api_error(409, "grant_limit_exceeded", f"单个文件最多授权 {settings.max_grants_per_file} 个用户")

    grant = FileGrant(
        file_id=record.id,
        grantee_sub=grantee,
        granted_by=sub,
        created_at=to_iso(utcnow()),
    )
    session.add(grant)
    audit(
        session,
        user_id=sub,
        action=ACTION_GRANT_ADD,
        target=record.id,
        result=RESULT_OK,
        detail={"grantee_sub": grantee, "visibility": record.visibility},
        request_ip=request_ip,
    )
    session.commit()
    return grant, True


def remove_grant(
    session: Session,
    *,
    file_id: str,
    sub: str,
    grantee_sub: str,
    request_ip: str | None = None,
) -> tuple[FileRecord, bool]:
    record = load_for_owner(session, file_id=file_id, sub=sub)
    grantee = str(grantee_sub or "").strip()
    grant = session.get(FileGrant, (record.id, grantee))
    if grant is None:
        return record, False
    session.delete(grant)
    audit(
        session,
        user_id=sub,
        action=ACTION_GRANT_REMOVE,
        target=record.id,
        result=RESULT_OK,
        detail={"grantee_sub": grantee},
        request_ip=request_ip,
    )
    session.commit()
    return record, True


# --------------------------------------------------------------- 对象丢失自愈


def mark_missing_object(session: Session, record: FileRecord, *, request_ip: str | None = None) -> None:
    """DB 有行、桶里没对象：标记删除并留痕，避免每次请求都去撞存储。"""
    record.status = STATUS_DELETED
    record.deleted_at = to_iso(utcnow())
    audit(
        session,
        user_id=record.user_id,
        action=ACTION_MISSING_OBJECT,
        target=record.id,
        result=RESULT_NOT_FOUND,
        detail={"storage_backend": record.storage_backend},
        request_ip=request_ip,
    )
    session.commit()


# --------------------------------------------------------------------- 清理


def cleanup_temp_files(settings: Settings, *, now: datetime | None = None) -> int:
    root = Path(settings.temp_root)
    if not root.is_dir():
        return 0
    cutoff = (now or utcnow()).timestamp() - settings.temp_file_max_age_seconds
    removed = 0
    for path in root.glob("tmp_*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:  # pragma: no cover - 并发删除/权限
            continue
    return removed


def sweep_once(session: Session, settings: Settings, storages: StorageRegistry) -> dict[str, int]:
    """一轮清理：过期 → 删对象 → 清暂存 → 清墓碑 →（local）清孤儿。"""
    now = utcnow()
    now_iso = to_iso(now)
    counts = {"expired": 0, "tombstones": 0, "temp_files": 0, "orphans": 0}

    rows = list(
        session.scalars(
            select(FileRecord)
            .where(FileRecord.status == STATUS_ACTIVE, FileRecord.expires_at <= now_iso)
            .order_by(FileRecord.expires_at)
            .limit(settings.cleanup_batch_size)
        ).all()
    )
    for record in rows:
        record.status = STATUS_EXPIRED
        record.expired_at = now_iso
        try:
            storages.for_record(record).delete(record.storage_key)
        except StorageError:
            logger.warning("过期对象删除失败: file=%s", record.id)
        audit(
            session,
            user_id=record.user_id,
            action=ACTION_EXPIRE,
            target=record.id,
            result=RESULT_OK,
            detail={"expires_at": record.expires_at},
            request_ip=None,
        )
        counts["expired"] += 1
    if rows:
        session.commit()

    counts["temp_files"] = cleanup_temp_files(settings, now=now)
    counts["tombstones"] = purge_tombstones(session, settings, now=now)
    if storages.primary.name == "local":
        counts["orphans"] = purge_orphans(session, settings, storages, dry_run=False)["deleted"]
    return counts


def purge_tombstones(session: Session, settings: Settings, *, now: datetime | None = None) -> int:
    """硬删超过保留期的墓碑行（授权行一并删掉，审计保留）。"""
    cutoff = to_iso((now or utcnow()) - timedelta(days=settings.tombstone_retention_days))
    conditions = or_(
        and_(FileRecord.status == STATUS_EXPIRED, FileRecord.expired_at.is_not(None), FileRecord.expired_at <= cutoff),
        and_(FileRecord.status == STATUS_DELETED, FileRecord.deleted_at.is_not(None), FileRecord.deleted_at <= cutoff),
    )
    ids = list(session.scalars(select(FileRecord.id).where(conditions).limit(settings.cleanup_batch_size)).all())
    if not ids:
        return 0
    session.execute(delete(FileGrant).where(FileGrant.file_id.in_(ids)))
    session.execute(delete(FileRecord).where(FileRecord.id.in_(ids)))
    session.commit()
    return len(ids)


def purge_orphans(
    session: Session,
    settings: Settings,
    storages: StorageRegistry,
    *,
    dry_run: bool = False,
    limit: int = 500,
) -> dict[str, int]:
    """删「桶里有、库里没有」的对象。

    * local 后端由清理器自动跑；
    * COS 后端**只在 CLI 显式执行**（``python -m app.cli sweep --orphans``）——
      列举桶是要花钱的请求，正常路径绝不能碰。

    只删「够老」的对象：上传流程是「先写对象、再写库」，太新的孤儿可能只是正在上传。
    """
    backend = storages.primary
    prefix = str(getattr(backend, "objects_prefix", "") or "")
    known = set(
        session.scalars(select(FileRecord.storage_key).where(FileRecord.storage_backend == backend.name)).all()
    )
    cutoff = utcnow() - timedelta(seconds=settings.temp_file_max_age_seconds)
    result = {"scanned": 0, "deleted": 0, "kept": 0}
    for ref in backend.iter_objects(prefix, limit=limit):
        result["scanned"] += 1
        if ref.key in known:
            result["kept"] += 1
            continue
        if ref.last_modified is not None and ref.last_modified > cutoff:
            result["kept"] += 1
            continue
        if not dry_run:
            try:
                backend.delete(ref.key)
            except StorageError:
                logger.warning("删除孤儿对象失败: key=%s", ref.key)
                continue
        result["deleted"] += 1
    return result


# --------------------------------------------------------------------- 统计


def stats_snapshot(session: Session, settings: Settings) -> dict[str, Any]:
    by_status = {str(status): int(count) for status, count in session.execute(select(FileRecord.status, func.count()).group_by(FileRecord.status)).all()}
    by_visibility = {
        str(visibility): int(count)
        for visibility, count in session.execute(
            select(FileRecord.visibility, func.count()).group_by(FileRecord.visibility)
        ).all()
    }
    users = int(session.scalar(select(func.count(func.distinct(FileRecord.user_id)))) or 0)
    grants = int(session.scalar(select(func.count()).select_from(FileGrant)) or 0)
    return {
        "files": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_visibility": by_visibility,
        },
        "users": users,
        "grants": grants,
        "capacity": global_quota_snapshot(session, settings),
        "storage_backend": settings.storage_backend,
        "download_mode": settings.download_mode,
        "cos_bucket": settings.cos_bucket if settings.storage_backend == "cos" else None,
    }
