"""搜索编排：并发打多源 → 归一化 → 去重 → 验活 → 排序。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from .adapters import REGISTRY
from .adapters import pansou as _pansou  # noqa: F401  触发注册
from .adapters import websearch as _websearch  # noqa: F401  触发注册
from .config import sources_config
from .dedupe import build_resources
from .models import PanType, RawHit, Resource, Status
from .score import score_all, sort_resources
from .verify import BaiduVerifier, verify_all

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class SearchOutcome:
    keyword: str
    resources: list[Resource] = field(default_factory=list)
    raw_hits: int = 0
    dedup_count: int = 0
    used_sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    verify_stats: dict = field(default_factory=dict)

    @property
    def alive_count(self) -> int:
        return sum(1 for r in self.resources if r.status.alive)


def build_adapters(names: list[str] | None = None):
    cfg = sources_config().get("sources") or {}
    adapters = []
    for name, scfg in cfg.items():
        if names and name not in names:
            continue
        if not scfg.get("enabled"):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            continue
        adapters.append(cls(scfg))
    return adapters


async def search(
    kw: str,
    *,
    types: list[PanType] | None = None,
    source_names: list[str] | None = None,
    do_verify: bool = True,
    alive_only: bool = False,
    limit: int | None = None,
) -> SearchOutcome:
    kw = kw.strip()
    adapters = build_adapters(source_names)
    outcome = SearchOutcome(keyword=kw, used_sources=[a.name for a in adapters])

    if not adapters:
        outcome.errors["_"] = "没有启用的数据源，请检查 config/sources.yaml"
        return outcome

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    ) as client:
        results = await asyncio.gather(
            *(a.search(kw, client) for a in adapters), return_exceptions=True
        )

        hits: list[RawHit] = []
        for adapter, result in zip(adapters, results):
            if isinstance(result, BaseException):
                outcome.errors[adapter.name] = f"{type(result).__name__}: {result}"
            else:
                hits.extend(result)
        outcome.raw_hits = len(hits)

        resources = build_resources(hits)
        outcome.dedup_count = len(resources)

        if types:
            allowed = set(types)
            resources = [r for r in resources if r.pan_type in allowed]

        if do_verify and resources:
            async with BaiduVerifier() as verifier:
                await verify_all(resources, verifier)
                outcome.verify_stats = dict(verifier.stats)

    score_all(resources, kw)
    resources = sort_resources(resources)

    if alive_only:
        # 百度只保留存活；非百度（未校验）保留，否则会把夸克/磁力全砍掉
        resources = [
            r
            for r in resources
            if r.status.alive
            or r.status in (Status.UNSUPPORTED, Status.UNCHECKED, Status.UNKNOWN)
        ]

    if limit:
        resources = resources[:limit]

    outcome.resources = resources
    return outcome
