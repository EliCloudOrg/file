"""后台清理 worker：过期 → 删对象 → 清暂存 → 清墓碑（local 后端顺带清孤儿）。

单容器单 uvicorn worker；SQLite 单写者。多副本需要把限流/清理锁挪到共享存储。
"""

from __future__ import annotations

import logging
import threading

from .config import Settings
from .db import get_session_factory
from .files_service import sweep_once
from .storage import StorageRegistry

logger = logging.getLogger("file.worker")


def run_cleanup(settings: Settings, storages: StorageRegistry, stop_event: threading.Event) -> None:
    logger.info("清理 worker 启动 interval=%ss", settings.cleanup_interval_seconds)
    while not stop_event.is_set():
        try:
            with get_session_factory()() as session:
                counts = sweep_once(session, settings, storages)
            if any(counts.values()):
                logger.info("清理完成 %s", counts)
        except Exception:  # noqa: BLE001 - worker 不能因为单次异常退出
            logger.exception("清理 worker 本轮失败")
        if stop_event.wait(settings.cleanup_interval_seconds):
            break
    logger.info("清理 worker 已停止")
