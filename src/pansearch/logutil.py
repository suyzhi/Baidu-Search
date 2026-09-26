"""统一日志配置。

为什么单独一个模块：这个项目此前**一处都没 import logging** —— Web 层把异常
catch 成一条推给客户端的文案就结束了，服务端零留痕（launchd 只兜住 stdout/stderr，
uvicorn 的 access log 只记请求，不记被 catch 掉的业务异常）。真出 bug 时表现是
"用户说搜不出来" + "日志里什么都没有"同时发生，等于瞎猜。

用法：
    from .logutil import get_logger
    logger = get_logger(__name__)

级别用环境变量 PANSEARCH_LOG_LEVEL 覆盖（默认 INFO）。
"""

from __future__ import annotations

import logging
import os
import sys

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVEL_ENV = "PANSEARCH_LOG_LEVEL"

_configured = False


def setup_logging(level: str | int | None = None, *, force: bool = False) -> None:
    """给 pansearch 这棵 logger 装一个 stderr handler（幂等）。

    刻意不动 root logger、也不调 basicConfig：Web 场景下 uvicorn 自己会配置
    uvicorn.* 的 handler，动 root 容易打架（日志重复，或者反过来被吞）。
    """
    global _configured
    if _configured and not force:
        return
    if level is None:
        level = os.environ.get(LEVEL_ENV, "INFO")
    if isinstance(level, str):
        resolved = getattr(logging, level.strip().upper(), None)
        level = resolved if isinstance(resolved, int) else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    root = logging.getLogger("pansearch")
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """取一个挂在 pansearch 下面的 logger（顺带保证 handler 已装好）。"""
    setup_logging()
    if name == "pansearch" or name.startswith("pansearch."):
        return logging.getLogger(name)
    return logging.getLogger(f"pansearch.{name}")


__all__ = ["get_logger", "setup_logging", "DEFAULT_FORMAT", "LEVEL_ENV"]
