"""FastAPI 应用装配。

服务内路由（对外由网关剥掉 `/file` 前缀）：

    POST   /v1/files                     GET  /v1/files
    GET    /v1/files/{id}                GET  /v1/files/{id}/content
    PATCH  /v1/files/{id}                DELETE /v1/files/{id}
    GET    /v1/files/{id}/grants         POST /v1/files/{id}/grants
    DELETE /v1/files/{id}/grants/{sub}   GET  /v1/me        GET /healthz

`/docs`、`/redoc`、`/openapi.json` 全部关掉（不把内部接口暴露到公网）。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, files_service
from .config import Settings, get_settings
from .constants import ACTION_DENY
from .db import get_session_factory, init_db
from .deps import client_ip
from .errors import limiter
from .routers import files, health
from .storage import StorageRegistry
from .worker import run_cleanup

logger = logging.getLogger("file")

# 400 也留痕，但按 IP 限流：有效令牌的客户端刷畸形请求不该刷爆审计表
INVALID_REQUEST_AUDIT_ATTEMPTS = 60
INVALID_REQUEST_AUDIT_WINDOW_SECONDS = 300

# multipart 自身有边界/头开销，预判时留一点余量，真正的上限由流式计数兜底
_MULTIPART_OVERHEAD = 64 * 1024


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).strip().upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _startup_warnings(settings: Settings, storages: StorageRegistry) -> None:
    if settings.storage_backend == "local":
        logger.warning("STORAGE_BACKEND=local：内容写在容器本地磁盘，仅适用于测试/本地开发")
    if settings.download_mode in ("presign", "both") and storages.primary.name != "cos":
        logger.warning(
            "DOWNLOAD_MODE=%s 但当前存储后端 %s 没有预签名能力：下载将一律走代理",
            settings.download_mode,
            storages.primary.name,
        )
    if not settings.required_read_scope or not settings.required_write_scope:
        logger.warning("REQUIRED_*_SCOPE 有空值：对应 scope 校验已关闭（只应在本地调试时如此）")
    expires = settings.capacity_expires_date
    if expires is not None:
        remaining = (expires - files_service.utcnow().date()).days
        if remaining <= 120:
            logger.warning(
                "COS 容量包将于 %s 到期（剩 %s 天）：到期前请续费或迁移，见 README-deploy.md",
                expires.isoformat(),
                remaining,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    storages: StorageRegistry = app.state.storages
    logger.info("file starting version=%s %s", __version__, settings.log_summary())
    _startup_warnings(settings, storages)

    stop_event: threading.Event | None = None
    thread: threading.Thread | None = None
    if settings.cleanup_enabled:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=run_cleanup,
            args=(settings, storages, stop_event),
            name="file-cleanup",
            daemon=True,
        )
        thread.start()
        app.state.cleanup_stop = stop_event
        app.state.cleanup_thread = thread

    try:
        yield
    finally:
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=5)
        logger.info("file stopped")


def _audit_invalid_request(request: Request, details: list[dict[str, Any]]) -> None:
    """请求体校验失败也留痕；best-effort，绝不改变那个 400 响应。"""
    ip = client_ip(request)
    if not limiter.allow(
        f"file-audit-400:{ip}", INVALID_REQUEST_AUDIT_ATTEMPTS, INVALID_REQUEST_AUDIT_WINDOW_SECONDS
    ):
        return
    try:
        with get_session_factory()() as session:
            files_service.audit(
                session,
                user_id=getattr(request.state, "user_id", None),
                action=ACTION_DENY,
                target=None,
                result="invalid_request",
                detail={
                    "path": request.url.path,
                    "errors": [str(item.get("msg")) for item in details][:5],
                },
                request_ip=ip,
            )
            session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能把 400 变成 500
        logger.exception("写 400 审计失败")


def create_app(settings: Settings | None = None, *, storage: Any | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    init_db(settings)
    storages = StorageRegistry(settings, primary=storage)

    app = FastAPI(
        title="EliCloud File Service",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.storages = storages

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 令牌原文绝不进日志；这里只记路径与耗时
            logger.exception("unhandled error path=%s", request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        user_id = getattr(request.state, "user_id", "-")
        logger.info(
            "%s %s -> %s sub=%s %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            user_id,
            elapsed_ms,
        )
        return response

    @app.middleware("http")
    async def upload_size_precheck(request: Request, call_next):
        """按 Content-Length 预判超大上传：先拒掉，不必把字节读进来再说。"""
        if request.method == "POST" and request.url.path.rstrip("/") == "/v1/files":
            raw = request.headers.get("content-length")
            if raw:
                try:
                    length = int(raw)
                except ValueError:
                    length = 0
                if length > settings.max_file_bytes + _MULTIPART_OVERHEAD:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": "file_too_large",
                            "error_description": f"文件超过上限 {settings.max_file_bytes} 字节",
                        },
                    )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {"error": "http_error", "error_description": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=content, headers=dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg"), "type": item.get("type")}
            for item in exc.errors()
        ]
        _audit_invalid_request(request, details)
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "error_description": "请求参数不合法", "details": details},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"error": "server_error", "error_description": "服务内部错误"},
        )

    app.include_router(files.router)
    app.include_router(health.router)

    return app


app = create_app()
