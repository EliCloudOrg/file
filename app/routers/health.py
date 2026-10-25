"""健康检查：无需令牌（容器 healthcheck 与网关探针用）。

语义：数据库可用 + 存储后端可用 → 200；任一不可用 → 503。
容量占满**不**算「不健康」（服务本身能用，只是拒绝新上传），但会带 ``warning``。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import __version__, files_service
from ..deps import DbDep, SettingsDep, StoragesDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz(settings: SettingsDep, storages: StoragesDep, db: DbDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    healthy = True

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception:  # noqa: BLE001 - 探针要给出结论，不能自己抛 500
        logger.exception("healthz: 数据库不可用")
        checks["database"] = {"ok": False, "error": "database_unavailable"}
        healthy = False

    try:
        checks["storage"] = storages.primary.health()
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = {"ok": False, "error": str(exc)}
        healthy = False

    try:
        checks["quota"] = files_service.global_quota_snapshot(db, settings)
    except Exception:  # noqa: BLE001 - 数据库已判失败时不再重复报错
        checks["quota"] = {"ok": False}

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "error",
            "version": __version__,
            "download_mode": settings.download_mode,
            "checks": checks,
        },
    )
