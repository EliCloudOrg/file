"""FastAPI 依赖：配置、数据库、存储注册表、Bearer 身份、scope、客户端 IP、限流。

身份来自**验签**（不是本服务签发）：只读令牌里的 claim，不查 SSO 数据库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import files_service
from .config import Settings, get_settings
from .constants import ACTION_DENY, RESULT_DENIED
from .db import get_db
from .errors import api_error, limiter
from .sso_auth import decode_access_token, get_jwks_cache, require_scope
from .storage import StorageRegistry

logger = logging.getLogger(__name__)

# 认证/授权失败也留痕，但按 IP 限流：坏令牌刷审计表的成本必须可控
AUTH_DENY_AUDIT_ATTEMPTS = 60
AUTH_DENY_AUDIT_WINDOW_SECONDS = 300

bearer_scheme = HTTPBearer(auto_error=False, description="SSO 签发的 RS256 Access Token")

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[DbSession, Depends(get_db)]


def get_storages(request: Request) -> StorageRegistry:
    storages = getattr(request.app.state, "storages", None)
    if storages is None:  # pragma: no cover - 只在装配错误时发生
        raise RuntimeError("应用尚未初始化存储后端")
    return storages


StoragesDep = Annotated[StorageRegistry, Depends(get_storages)]


@dataclass(frozen=True)
class Identity:
    """已验签的调用方身份。"""

    sub: str
    username: str | None
    claims: dict[str, Any]

    @property
    def scope(self) -> str:
        return str(self.claims.get("scope") or "")


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    网关（Caddy）把客户端 IP **追加**到 X-Forwarded-For 末尾，所以取最后一段；
    取第一段会被调用方自带的值污染。服务不直接对公网暴露端口，这是可接受的前提。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1]
    if request.headers.get("x-real-ip"):
        return str(request.headers["x-real-ip"]).strip()
    return request.client.host if request.client else "unknown"


def _audit_auth_denied(
    db: DbSession,
    request: Request,
    *,
    user_id: str | None,
    detail: dict[str, Any],
) -> None:
    ip = client_ip(request)
    if not limiter.allow(f"file-auth-deny:{ip}", AUTH_DENY_AUDIT_ATTEMPTS, AUTH_DENY_AUDIT_WINDOW_SECONDS):
        return
    try:
        files_service.audit(
            db,
            user_id=user_id,
            action=ACTION_DENY,
            target=None,
            result=RESULT_DENIED,
            detail=detail,
            request_ip=ip,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能把 401/403 变成 500
        db.rollback()
        logger.exception("写认证拒绝审计失败")


def current_identity(
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Identity:
    """只做验签（所有 `/v1/*` 都挂它）；scope 由 `require_read` / `require_write` 再卡。"""
    claims: dict[str, Any] | None = None
    try:
        if credentials is None or not credentials.credentials:
            raise api_error(401, "invalid_token", "缺少 Bearer 令牌", headers={"WWW-Authenticate": "Bearer"})
        if credentials.scheme.lower() != "bearer":
            raise api_error(401, "invalid_token", "认证方案必须是 Bearer", headers={"WWW-Authenticate": "Bearer"})
        claims = decode_access_token(settings, get_jwks_cache(settings), credentials.credentials)
    except StarletteHTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "denied", "error_description": str(exc.detail)}
        _audit_auth_denied(db, request, user_id=None, detail={"status": exc.status_code, **detail})
        raise

    sub = str(claims.get("sub"))
    request.state.user_id = sub
    username = claims.get("preferred_username") or claims.get("username")
    return Identity(sub=sub, username=str(username) if username else None, claims=claims)


IdentityDep = Annotated[Identity, Depends(current_identity)]


def _scope_gate(
    identity: Identity,
    settings: Settings,
    db: DbSession,
    request: Request,
    *,
    required_scope: str,
) -> Identity:
    try:
        require_scope(identity.claims, required_scope)
    except StarletteHTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "denied", "error_description": str(exc.detail)}
        _audit_auth_denied(db, request, user_id=identity.sub, detail={"status": exc.status_code, **detail})
        raise
    return identity


def require_read(
    request: Request,
    identity: IdentityDep,
    settings: SettingsDep,
    db: DbDep,
) -> Identity:
    return _scope_gate(identity, settings, db, request, required_scope=settings.required_read_scope)


def require_write(
    request: Request,
    identity: IdentityDep,
    settings: SettingsDep,
    db: DbDep,
) -> Identity:
    return _scope_gate(identity, settings, db, request, required_scope=settings.required_write_scope)


ReadIdentity = Annotated[Identity, Depends(require_read)]
WriteIdentity = Annotated[Identity, Depends(require_write)]


def check_write_rate_limit(*, settings: Settings, user_id: str, ip: str) -> None:
    """写操作按 `sub` + IP 双维度限流，防脚本刷上传/删除。"""
    limit = settings.upload_attempts_per_window
    window = settings.upload_window_seconds
    for key in (f"file-write:{user_id}", f"file-write-ip:{ip}"):
        if not limiter.allow(key, limit, window):
            raise api_error(
                429,
                "rate_limited",
                "操作过于频繁，请稍后再试",
                headers={"Retry-After": str(window)},
            )
