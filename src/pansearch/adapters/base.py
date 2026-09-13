"""适配器基类与注册表：新增数据源 = 加一个文件 + 在 sources.yaml 打开开关。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextvars import ContextVar

import httpx

from ..models import RawHit

REGISTRY: dict[str, type["Adapter"]] = {}
# 每个源/查询独立的收集器，子协程继承；超时仍可取回已完成的页面。
partial_hits: ContextVar[list[RawHit] | None] = ContextVar("partial_hits", default=None)


def publish_hits(hits: list[RawHit]) -> list[RawHit]:
    collector = partial_hits.get()
    if collector is not None:
        collector.extend(hits)
    return hits


def register(cls: type["Adapter"]) -> type["Adapter"]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    REGISTRY[cls.name] = cls
    return cls


class Adapter(ABC):
    name: str = ""
    kind: str = ""

    # 只对"主查询/别名查询"运行，跳过放宽补搜查询。
    # 慢源跑一遍要好几秒，而补搜词本身噪声很大（实测 "模板" 只回 8 条却要 6 秒），
    # 让它们跟着补搜一起跑是纯浪费。
    primary_only: bool = False

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def weight(self) -> float:
        return float(self.cfg.get("weight") or 0.6)

    @abstractmethod
    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        """返回该源关于关键词的全部原始命中。"""
        raise NotImplementedError
