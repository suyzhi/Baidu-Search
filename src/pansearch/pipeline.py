"""搜索编排：并发打多源 → 归一化 → 去重 → 验活 → 排序。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx

from .adapters import REGISTRY
from .adapters import pansou as _pansou  # noqa: F401  触发注册
from .adapters import websearch as _websearch  # noqa: F401  触发注册
from .config import sources_config
from .dedupe import build_resources
from .models import PanType, RawHit, Resource
from .score import score_all, sort_resources
from .verifiers import VerifierPool, prune

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 原始命中少于此值时，自动用"放宽查询"补一次召回。
# 实测多词查询（如「沙丘 4K HDR」）在聚合引擎只有个位数~几十条，
# 而主词（「沙丘」）有 180+ 条，所以阈值给宽一点。
RELAX_THRESHOLD = 60
_TERM_SPLIT = re.compile(r"[\s,，、/|·]+")


def relaxed_queries(kw: str) -> list[str]:
    """从多词查询派生放宽查询。

    PanSou 这类聚合引擎对多词查询召回很差：实测「沙丘 4K HDR」只有 3 条，
    而「沙丘」有 180+ 条。所以原始查询召回不足时，补搜主词与剩余词。
    """
    parts = [p for p in _TERM_SPLIT.split(kw.strip()) if p]
    if len(parts) < 2:
        return []
    candidates = [parts[0]]
    tail = " ".join(parts[1:]).strip()
    if tail:
        candidates.append(tail)
    return [q for q in dict.fromkeys(candidates) if q and q != kw.strip()]


@dataclass
class SearchOutcome:
    keyword: str
    resources: list[Resource] = field(default_factory=list)
    raw_hits: int = 0
    dedup_count: int = 0
    pruned: int = 0
    strict: bool = False
    used_sources: list[str] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
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


async def _fetch_hits(
    adapters, client: httpx.AsyncClient, query: str
) -> tuple[list[RawHit], dict[str, str]]:
    """并发打所有数据源，单源失败隔离。"""
    results = await asyncio.gather(
        *(a.search(query, client) for a in adapters), return_exceptions=True
    )
    hits: list[RawHit] = []
    errors: dict[str, str] = {}
    for adapter, result in zip(adapters, results):
        if isinstance(result, BaseException):
            errors[adapter.name] = f"{type(result).__name__}: {result}"
        else:
            hits.extend(result)
    return hits, errors


async def search(
    kw: str,
    *,
    types: list[PanType] | None = None,
    source_names: list[str] | None = None,
    do_verify: bool = True,
    alive_only: bool = False,
    strict: bool = False,
    relax: bool = True,
    limit: int | None = None,
) -> SearchOutcome:
    kw = kw.strip()
    adapters = build_adapters(source_names)
    outcome = SearchOutcome(keyword=kw, used_sources=[a.name for a in adapters])
    outcome.queries_used = [kw]
    outcome.strict = strict

    if not adapters:
        outcome.errors["_"] = "没有启用的数据源，请检查 config/sources.yaml"
        return outcome

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    ) as client:
        hits, errors = await _fetch_hits(adapters, client, kw)
        outcome.errors.update(errors)

        # 多词查询召回塌陷时，用放宽查询补召回（补搜结果会被降权，不会淹没主结果）
        if relax and len(hits) < RELAX_THRESHOLD:
            for alt in relaxed_queries(kw):
                more, alt_errors = await _fetch_hits(adapters, client, alt)
                for name, err in alt_errors.items():
                    outcome.errors.setdefault(name, err)
                if more:
                    for hit in more:
                        hit.relaxed = True
                    hits.extend(more)
                    outcome.queries_used.append(alt)
                if len(hits) >= RELAX_THRESHOLD * 4:
                    break

        outcome.raw_hits = len(hits)

        resources = build_resources(hits)
        outcome.dedup_count = len(resources)

        if types:
            allowed = set(types)
            resources = [r for r in resources if r.pan_type in allowed]

        if do_verify and resources:
            async with VerifierPool() as pool:
                await pool.verify_all(resources)
                outcome.verify_stats = dict(pool.stats)

    score_all(resources, kw)
    resources = sort_resources(resources)

    if alive_only:
        # 剔除失效链接：验活过的按状态剔除；不支持验活的网盘默认保留
        resources, outcome.pruned = prune(resources, strict=strict)

    if limit:
        resources = resources[:limit]

    outcome.resources = resources
    return outcome
