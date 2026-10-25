"""表结构（SQLite，TEXT + ISO8601，与 SSO / mc 同风格）。

三张表（规格见计划 §4.1）：

* ``files``       —— 一行 = 一个文件（元数据；内容在 COS/本地磁盘）
* ``file_grants`` —— protected 文件的授权（被授权者的 SSO ``sub``）
* ``audit_logs``  —— 全量审计（成功与失败都留痕，与 mc 同名同形）

设计要点：

1. ``storage_backend`` 按行记录（``local``/``cos``）：换后端后旧行仍能按原后端读出来，
   而不是「一改配置，历史文件全 404」。
2. 墓碑不立刻硬删：``deleted``/``expired`` 行保留 ``TOMBSTONE_RETENTION_DAYS`` 天，
   期间对象已删但 id 不会被复用，便于排障与审计。
3. 「对象 key 由 id 派生」，不用用户输入拼路径 —— 从根上避免路径穿越。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .constants import (
    ISO_FORMAT,
    PERMANENT_EXPIRES_AT,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    VISIBILITY_PRIVATE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def dumps_detail(payload: dict[str, object] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_user_status_created", "user_id", "status", "created_at"),
        Index("ix_files_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # f_<22>
    user_id: Mapped[str] = mapped_column(Text, nullable=False)  # SSO sub（不建外键：不跨库）
    filename: Mapped[str] = mapped_column(Text, nullable=False)  # 已净化，仅展示
    content_type: Mapped[str] = mapped_column(Text, nullable=False)  # 生效类型（危险类型已降级）
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)  # ETag 与完整性基准
    storage_backend: Mapped[str] = mapped_column(Text, nullable=False)  # local | cos
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)  # 对象 key / 相对路径
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default=VISIBILITY_PRIVATE)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=STATUS_ACTIVE)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    expired_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------ 派生状态
    @property
    def is_permanent(self) -> bool:
        """``expires_at`` 的哨兵值表示永久；对外不暴露哨兵。"""
        return self.expires_at == PERMANENT_EXPIRES_AT

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.is_permanent:
            return False
        return parse_iso(self.expires_at) <= (now or utcnow())

    def effective_status(self, now: datetime | None = None) -> str:
        """``status`` 存的是「清理器走到哪一步」，对外要按时间算真实状态。

        清理器每 ``CLEANUP_INTERVAL_SECONDS`` 才跑一轮，中间这段时间里
        ``status`` 还是 active 但 ``expires_at`` 已过 —— 对外必须已经是 expired。
        """
        if self.status == STATUS_ACTIVE and self.is_expired(now):
            return STATUS_EXPIRED
        return self.status

    def to_public_dict(self, *, now: datetime | None = None, grant_count: int | None = None) -> dict[str, object]:
        moment = now or utcnow()
        status = self.effective_status(moment)
        permanent = self.is_permanent
        remaining = None if permanent else int((parse_iso(self.expires_at) - moment).total_seconds())
        payload: dict[str, object] = {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "status": status,
            "visibility": self.visibility,
            "created_at": self.created_at,
            "expires_at": None if permanent else self.expires_at,
            "expires_in": None if permanent else max(remaining or 0, 0),
            "permanent": permanent,
            "content_path": f"/v1/files/{self.id}/content",
        }
        if grant_count is not None:
            payload["grant_count"] = grant_count
        return payload


class FileGrant(Base):
    __tablename__ = "file_grants"
    __table_args__ = (Index("ix_file_grants_grantee", "grantee_sub"),)

    file_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    grantee_sub: Mapped[str] = mapped_column(Text, primary_key=True)
    granted_by: Mapped[str] = mapped_column(Text, nullable=False)  # 属主 sub（审计冗余）
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "grantee_sub": self.grantee_sub,
            "created_at": self.created_at,
        }


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # 失败且未认证时为空
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)  # 文件 id
    result: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON（不含内容/令牌）
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sub": self.user_id,
            "action": self.action,
            "target": self.target,
            "result": self.result,
            "detail": json.loads(self.detail) if self.detail else None,
            "request_ip": self.request_ip,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<AuditLog {self.action} {self.result} sub={self.user_id}>"
