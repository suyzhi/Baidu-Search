"""通用小工具。"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """简单令牌桶：保证对同一目标的请求间隔不低于 1/qps 秒。"""

    def __init__(self, qps: float):
        self.interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = now + self.interval
