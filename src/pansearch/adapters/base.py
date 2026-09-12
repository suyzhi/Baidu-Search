"""适配器基类与注册表：新增数据源 = 加一个文件 + 在 sources.yaml 打开开关。"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..models import RawHit

REGISTRY: dict[str, type["Adapter"]] = {}


def register(cls: type["Adapter"]) -> type["Adapter"]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    REGISTRY[cls.name] = cls
    return cls


class Adapter(ABC):
    name: str = ""
    kind: str = ""

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
