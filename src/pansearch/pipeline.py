"""搜索编排：并发打多源 → 归一化 → 去重 → 验活 → 排序。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx

from .adapters import REGISTRY
from .adapters import apisources as _apisources  # noqa: F401  触发注册
from .adapters import pansou as _pansou  # noqa: F401  触发注册
from .adapters import sitesearch as _sitesearch  # noqa: F401  触发注册
from .adapters import telegram as _telegram  # noqa: F401  触发注册
from .adapters import websearch as _websearch  # noqa: F401  触发注册
from .config import alias_config, sources_config, verify_cfg
from .dedupe import build_resources
from .models import PanType, RawHit, Resource, Status, VerifyResult
from .score import score_all, sort_resources
from .verifiers import VerifierPool, prune

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_TERM_SPLIT = re.compile(r"[\s,，、/|·]+")

# 补搜词的最小长度：单字符没有区分度
MIN_RELAX_TERM_LEN = 2


def _usable_relaxed_term(term: str) -> bool:
    """补搜词必须真的有区分度，否则不如不补。

    实测踩过的坑：「沙丘 2」会拆出补搜词 "2"，而本地 TG 索引里有 18.4 万条消息
    含 "2"（占 87%）—— 等于拿无意义的词全库扫描，白白拉长搜索时间，
    还会把大量不相关结果塞进验活预算。
    """
    t = term.strip()
    if len(t) < MIN_RELAX_TERM_LEN:
        return False
    if not any(ch.isalnum() for ch in t):   # 纯符号
        return False
    if t.isdigit():                          # "2" / "2024" 这类纯数字
        return False
    return True


def relaxed_queries(kw: str) -> list[str]:
    """从多词查询派生放宽查询。

    PanSou 这类聚合引擎对多词查询召回很差：实测「沙丘 4K HDR」只有 3 条，
    而「沙丘」有 180+ 条。所以主查询之余补搜主词与限定词 —— 但要过滤掉
    "2"、"2024" 这种毫无区分度的词（见 _usable_relaxed_term）。
    """
    parts = [p for p in _TERM_SPLIT.split(kw.strip()) if p]
    if len(parts) < 2:
        return []

    candidates: list[str] = []
    if _usable_relaxed_term(parts[0]):
        candidates.append(parts[0])
    tail = " ".join(p for p in parts[1:] if _usable_relaxed_term(p)).strip()
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
    verify_budget_skipped: int = 0
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


def alias_queries(kw: str) -> list[str]:
    """把查询里的中文俗称替换成实际检索词。

    实测「大气合成器」原始命中 0 条，而同一件事搜 "Omnisphere" 能出 20+ 条 ——
    资源站和聚合引擎里只有英文名，中文用户搜的是社区俗称。
    这类替换**不降权**（它是等价替换，不是放宽），命中结果按替换后的词打分。
    """
    aliases = alias_config()
    if not aliases:
        return []
    low = kw.strip().lower()
    out: list[str] = []
    for alias, targets in aliases.items():
        a = str(alias).strip().lower()
        if not a or a not in low:
            continue
        for target in targets or []:
            replaced = low.replace(a, str(target).strip().lower())
            if replaced and replaced != low:
                out.append(replaced)
    return list(dict.fromkeys(out))


async def _fetch_hits(
    adapters, client: httpx.AsyncClient, query: str, *, deadline: float | None = None
) -> tuple[list[RawHit], dict[str, str]]:
    """并发打所有数据源，**每个源有自己的截止时间**，超时就跳过。

    实测各源耗时差异极大（TG 本地索引 0.0s / PanSou 8~25s / Brave 15~22s）。
    一刀切的截止时间会误伤：定 15s 时 PanSou 常被砍掉，而定 30s 又要为低产出的
    Brave 白等。所以按各源自己的 `deadline` 配置（缺省用全局 fetch_deadline）。
    """
    default = float(deadline or sources_config().get("fetch_deadline") or 15.0)

    async def one(adapter) -> list[RawHit]:
        limit = float(adapter.cfg.get("deadline") or default)
        try:
            return await asyncio.wait_for(adapter.search(query, client), timeout=limit)
        except asyncio.TimeoutError:
            raise RuntimeError(f"超时（>{limit:.0f}s），已跳过") from None

    results = await asyncio.gather(*(one(a) for a in adapters), return_exceptions=True)

    hits: list[RawHit] = []
    errors: dict[str, str] = {}
    for adapter, result in zip(adapters, results):
        if isinstance(result, BaseException):
            errors[adapter.name] = (
                str(result) if isinstance(result, RuntimeError)
                else f"{type(result).__name__}: {result}"
            )
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
    verify_budget: int | None = None,
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
        # 主查询 / 补搜 / 别名替换 一起并行发出：
        #   补搜（relaxed=True）要降权 —— 它是放宽，召回多但精准度低
        #   别名（relaxed=False）不降权 —— 它是等价替换，命中结果按替换后的词打分
        plan: list[tuple[str, bool]] = [(kw, False)]
        if relax:
            plan += [(q, True) for q in relaxed_queries(kw)]
            plan += [(q, False) for q in alias_queries(kw)]
        batches = await asyncio.gather(
            *(
                _fetch_hits(
                    [a for a in adapters if not (a.primary_only and is_relaxed)]
                    or adapters,
                    client, q,
                )
                for q, is_relaxed in plan
            ),
            return_exceptions=True,
        )

        hits: list[RawHit] = []
        for (query, is_relaxed), batch in zip(plan, batches):
            if isinstance(batch, BaseException):
                outcome.errors.setdefault("_", f"{type(batch).__name__}: {batch}")
                continue
            got, errs = batch
            for name, err in errs.items():
                outcome.errors.setdefault(name, err)
            for hit in got:
                hit.relaxed = is_relaxed
                hit.query = query
            if query != kw and got:
                outcome.queries_used.append(query)
            hits.extend(got)

        outcome.raw_hits = len(hits)

        resources = build_resources(hits)
        outcome.dedup_count = len(resources)

        if types:
            allowed = set(types)
            resources = [r for r in resources if r.pan_type in allowed]

        if do_verify and resources:
            # ---- 验活预算：先按相关性排序，只验活最相关的前 N 条 ----
            # 大结果集（700+）全量验活要 1~2 分钟，而用户只看前几十条。
            budget = verify_budget if verify_budget is not None else verify_cfg().get("budget", 300)
            budget = int(budget or 0)
            if budget > 0 and len(resources) > budget:
                score_all(resources, kw)
                resources.sort(key=lambda r: -r.score)
                head, tail = resources[:budget], resources[budget:]
                outcome.verify_budget_skipped = len(tail)
                for res in tail:
                    res.verify = VerifyResult(
                        status=Status.UNCHECKED,
                        note=f"超出验活预算（仅验活最相关的前 {budget} 条）",
                    )
                resources = head
            else:
                tail = []

            async with VerifierPool() as pool:
                await pool.verify_all(resources)
                outcome.verify_stats = dict(pool.stats)
            resources = resources + tail

    score_all(resources, kw)
    resources = sort_resources(resources)

    if alive_only:
        # 剔除失效链接：验活过的按状态剔除；不支持验活的网盘默认保留
        resources, outcome.pruned = prune(resources, strict=strict)

    if limit:
        resources = resources[:limit]

    outcome.resources = resources
    return outcome
