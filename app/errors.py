"""统一的错误响应结构与限流（与 `sso/app/errors.py`、`mc/app/errors.py` 同构）。

错误结构：``{"error": "...", "error_description": "..."}``（400 另带 ``details``）。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException


def api_error(
    status_code: int,
    error: str,
    description: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": error, "error_description": description},
        headers=headers,
    )


def storage_unavailable(description: str = "对象存储暂时不可用，请稍后重试") -> HTTPException:
    """存储后端不可达/凭据错误时的统一错误：**503**（fail closed，绝不降级写本地）。"""
    return api_error(503, "storage_unavailable", description, headers={"Retry-After": "30"})


class RateLimiter:
    """进程内滑动窗口计数器：key -> 时间戳队列。

    单进程部署（一个 uvicorn 进程）足够；多副本时各副本独立计数，属已知限制。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float, *, now: float | None = None) -> bool:
        """记录一次尝试；未超限返回 True。"""
        if limit <= 0:
            return True
        moment = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits[key]
            cutoff = moment - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(moment)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
