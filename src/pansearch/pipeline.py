"""搜索编排：并发打多源 → 归一化 → 去重 → 验活 → 排序。"""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from copy import copy, deepcopy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from .adapters import REGISTRY
from .adapters import apisources as _apisources  # noqa: F401  触发注册
from .adapters import bilibili as _bilibili  # noqa: F401  触发注册
from .adapters import btsearch as _btsearch  # noqa: F401  触发注册
from .adapters import pansou as _pansou  # noqa: F401  触发注册
from .adapters import sitesearch as _sitesearch  # noqa: F401  触发注册
from .adapters import telegram as _telegram  # noqa: F401  触发注册
from .adapters import websearch as _websearch  # noqa: F401  触发注册
from .adapters.base import partial_hits
from .config import alias_config, cfg_float, cfg_int, sources_config, verify_cfg
from .dedupe import build_resources
from .models import PanType, RawHit, Resource
from .query import MODIFIERS, normalize_text, split_query
from .score import confidently_irrelevant, score_all, sort_resources
from .store import VerifyCache
from .verifiers import VerifierPool, prune

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 查询切词统一走 query.split_query / query_terms（见 query.py），这里不再自建正则。

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
    if t.isdigit() or normalize_text(t) in MODIFIERS:
        return False
    return True


def relaxed_queries(kw: str) -> list[str]:
    """从多词查询派生放宽查询。

    PanSou 这类聚合引擎对多词查询召回很差：实测「沙丘 4K HDR」只有 3 条，
    而「沙丘」有 180+ 条。所以补搜有区分度的词，跳过纯画质、格式和数字。
    """
    parts = split_query(kw)
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
    verify_timeout_skipped: int = 0
    irrelevant_pruned: int = 0
    # --sfw 时被剔掉的成人站结果数（用于在摘要里如实说明"过滤了多少"）
    nsfw_pruned: int = 0
    used_sources: list[str] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # 每个数据源的状态/耗时/产出；degraded 是失败、超时或部分失败的源。
    # 有了它，"结果少"才能区分成"确实没有"还是"某个源挂了"—— 不允许静默降级。
    source_report: dict[str, dict] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    # --sfw 时被过滤掉的成人源命中数（如实展示，避免"结果变少但不知道为什么"）
    adult_pruned: int = 0
    # 本次结果是否来自查询级缓存（重复搜索稳定、且省掉一次全网抓取）
    from_cache: bool = False
    verify_stats: dict = field(default_factory=dict)
    # 分阶段耗时（秒）。只靠"总耗时"没法判断该优化哪一段 ——
    # 实测多次以为瓶颈在验活，实际在抓取阶段。
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def alive_count(self) -> int:
        return sum(1 for r in self.resources if r.status.alive)

    @property
    def slowest_stage(self) -> str:
        if not self.timings:
            return ""
        stages = {k: v for k, v in self.timings.items() if k != "total"}
        return max(stages, key=stages.get) if stages else ""


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
    # 先去标点再匹配/替换：「《大气合成器》」「大气合成器!」这类也要能命中别名。
    low = " ".join(split_query(kw)).casefold()
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


_STATUS_RANK = {"ok": 0, "empty": 1, "degraded": 2, "timeout": 3, "error": 4}

# 查询级缓存：同一关键词 + 同一组参数在 TTL 内直接复用整份结果。
# 意义不是省 CPU，而是**一致性** —— 同一个搜索不会因为某次网络抖动给出不同的答案，
# 这也是"可靠"最容易被用户感知的一面。进程内、容量有限、命中返回深拷贝（防串改）。
_QUERY_CACHE: dict[str, tuple[float, "SearchOutcome"]] = {}
_QUERY_CACHE_MAX = 64


def clear_query_cache() -> None:
    _QUERY_CACHE.clear()


def _query_cache_key(kw, types, source_names, do_verify, alive_only, strict, relax,
                     verify_budget, sfw: bool = False) -> str:
    return "|".join([
        kw,
        ",".join(sorted(t.value for t in (types or []))),
        ",".join(sorted(source_names or [])),
        str(bool(do_verify)), str(bool(alive_only)), str(bool(strict)), str(bool(relax)),
        str(verify_budget), str(bool(sfw)),
    ])


def adult_sources() -> tuple[str, ...]:
    """config/sources.yaml 里标记为成人内容的来源标签。"""
    raw = sources_config().get("adult_sources") or []
    return tuple(str(s).lower() for s in raw if str(s).strip())


def is_adult_source(source: str | None) -> bool:
    low = str(source or "").lower()
    return any(marker in low for marker in adult_sources())


def filter_adult_hits(hits: list[RawHit]) -> tuple[list[RawHit], int]:
    """--sfw：按来源过滤成人源命中，返回 (保留的命中, 过滤掉的条数)。

    按"来源"而不是按标题关键词过滤：javdb 这类源返回的**就是**成人内容，
    靠标题猜既会漏（不同语言/缩写）也会误伤（"写真集""深夜剧"这类正常资源）。
    """
    if not adult_sources():
        return hits, 0
    kept = [h for h in hits if not is_adult_source(h.source)]
    return kept, len(hits) - len(kept)


def _merge_report(acc: dict[str, dict], rep: dict[str, dict]) -> None:
    """把一次查询的源状态并入总表：累计产出、取最大耗时、取最差状态。

    只要拿到了结果，最差也只能是 degraded —— 一次补搜超时不该让主力源显示成"挂了"。
    """
    for name, r in rep.items():
        cur = acc.get(name)
        if cur is None:
            acc[name] = dict(r)
            continue
        cur["hits"] += r["hits"]
        cur["seconds"] = round(max(cur["seconds"], r["seconds"]), 2)
        if _STATUS_RANK[r["status"]] > _STATUS_RANK.get(cur["status"], 0):
            cur["status"] = r["status"]
        if r.get("error") and not cur.get("error"):
            cur["error"] = r["error"]
        if cur["hits"] > 0 and cur["status"] in ("error", "timeout"):
            cur["status"] = "degraded"


async def _fetch_hits(
    adapters, client: httpx.AsyncClient, query: str, *, deadline: float | None = None
) -> tuple[list[RawHit], dict[str, str], dict[str, dict]]:
    """并发打所有数据源，**每个源有自己的截止时间**，超时就跳过。

    实测各源耗时差异极大（TG 本地索引 0.0s / PanSou 8~25s / Brave 15~22s）。
    一刀切的截止时间会误伤：定 15s 时 PanSou 常被砍掉，而定 30s 又要为低产出的
    Brave 白等。所以按各源自己的 `deadline` 配置（缺省用全局 fetch_deadline）。
    """
    # 调用方显式给了 deadline 就用它；否则读全局 fetch_deadline（0 也是合法值，
    # 所以不能再写 "or 15.0"）
    default = (float(deadline) if deadline is not None
               else cfg_float(sources_config(), "fetch_deadline", 15.0))

    async def one(adapter) -> tuple[list[RawHit], str | None, float]:
        limit = cfg_float(adapter.cfg, "deadline", default)
        collected: list[RawHit] = []
        token = partial_hits.set(collected)
        got: list[RawHit] | None = None
        t0 = time.monotonic()
        try:
            got = await asyncio.wait_for(adapter.search(query, client), timeout=limit)
            error = None
        except asyncio.TimeoutError:
            got, error = collected, f"超时（>{limit:g}s），保留已取得的 {len(collected)} 条命中"
        except Exception as exc:
            got, error = collected, f"{type(exc).__name__}: {exc}"
        finally:
            partial_hits.reset(token)
        # 记录**该来源内部**的名次，供 RRF 多源融合使用（来源不排序则名次无意义，
        # 但代价只是一个整数，不影响既有打分）。
        for i, hit in enumerate(got):
            try:
                hit.rank = i
            except (AttributeError, ValueError):
                pass
        return got, error, time.monotonic() - t0

    results = await asyncio.gather(*(one(a) for a in adapters), return_exceptions=True)

    hits: list[RawHit] = []
    errors: dict[str, str] = {}
    report: dict[str, dict] = {}
    for adapter, result in zip(adapters, results):
        if isinstance(result, BaseException):
            errors[adapter.name] = (
                str(result) if isinstance(result, RuntimeError)
                else f"{type(result).__name__}: {result}"
            )
            report[adapter.name] = {"status": "error", "hits": 0, "seconds": 0.0,
                                    "error": errors[adapter.name]}
            continue
        got, error, secs = result
        hits.extend(got)
        if error:
            errors[adapter.name] = error
        if error and not got:
            status = "timeout" if "超时" in error else "error"
        elif error:
            status = "degraded"          # 部分成功：拿到了结果，但源本身报错/超时
        elif got:
            status = "ok"
        else:
            status = "empty"
        report[adapter.name] = {"status": status, "hits": len(got),
                                "seconds": round(secs, 2), "error": error}
    return hits, errors, report


def nsfw_markers() -> tuple[str, ...]:
    """成人内容源的标记（config/sources.yaml 的 nsfw_sources）。

    做法是**按来源标记而不是按关键词猜**：javdb 这种源返回的就是成人内容，
    打标记最准确；靠标题关键词判断既会漏也会误伤（"写真集"之类的正常资源）。
    """
    return tuple(str(m).lower() for m in (sources_config().get("nsfw_sources") or []))


def is_nsfw(resource: Resource) -> bool:
    markers = nsfw_markers()
    if not markers:
        return False
    for label in [*resource.sources, *resource.kinds]:
        low = str(label).lower()
        if any(m in low for m in markers):
            return True
    return False


# ---- 流式预览节流 ----
# "先显示，再补全"原本是：**任意一个**"数据源 × 查询"任务一返回就重跑一遍
# build_resources（去重）+ score_all（打分）再推一帧。一次搜索可能派发 20 个任务，
# 而 TG 本地索引 0.02s 就能返回几百条 —— 结果密集到达时等于在**同一次搜索里**把
# 去重+打分完整重跑 20 次，且一次比一次数据量大。CPU 花在重复劳动上，结果量上千时
# 反而让"先显示"的这几帧变卡（这些计算在 to_thread 里，不阻塞抓取，但抢 CPU）。
#
# 两个放行条件互相独立：间隔保证"慢慢到货"时界面也在动，新增条数保证大结果集
# 不会长时间停在旧的一屏。首帧永远放行。
PREVIEW_MIN_INTERVAL_SECONDS = 0.4
PREVIEW_MIN_NEW_HITS = 25


class PreviewThrottle:
    """判断这一批新命中值不值得重算并推送一帧预览。"""

    def __init__(self, min_interval: float = PREVIEW_MIN_INTERVAL_SECONDS,
                 min_new_hits: int = PREVIEW_MIN_NEW_HITS):
        self.min_interval = max(0.0, float(min_interval))
        self.min_new_hits = max(1, int(min_new_hits))
        self.last_at: float | None = None
        self.last_hits = 0
        self.emitted = 0
        self.skipped = 0

    def should_emit(self, now: float, hits: int) -> bool:
        if self.last_at is None:          # 首帧：界面尽快有东西看
            return True
        if hits - self.last_hits >= self.min_new_hits:
            return True
        return (now - self.last_at) >= self.min_interval

    def mark(self, now: float, hits: int) -> None:
        self.last_at, self.last_hits = now, hits
        self.emitted += 1


def _prepare(outcome: SearchOutcome, hits: list[RawHit], equivalents: list[str],
             types: list[PanType] | None, alive_only: bool, sfw: bool = False) -> None:
    outcome.raw_hits = len(hits)
    resources = build_resources(hits)
    outcome.dedup_count = len(resources)
    if sfw:
        kept = [r for r in resources if not is_nsfw(r)]
        outcome.nsfw_pruned = len(resources) - len(kept)
        resources = kept
    if types:
        allowed = set(types)
        resources = [r for r in resources if r.pan_type in allowed]
    for res in resources:
        res.queries = list(dict.fromkeys([*res.queries, *equivalents]))
    score_all(resources, outcome.keyword)
    if alive_only:
        kept = [r for r in resources if not confidently_irrelevant(r, outcome.keyword)]
        outcome.irrelevant_pruned = len(resources) - len(kept)
        resources = kept
    outcome.resources = resources


# 边抓边验只送"匹配程度最高一档"的结果：此时还不知道全局排名，
# 低相关的留给收尾阶段按全局相关性分配预算。
EARLY_VERIFY_MIN_RELEVANCE = 0.85


def _early_candidates(hits: list[RawHit], kw: str, equivalents: list[str],
                      types: list[PanType] | None, sfw: bool) -> list[Resource]:
    """一批新到的命中里值得立即验活的资源，按相关性从高到低。"""
    resources = build_resources(hits)
    if sfw:
        resources = [r for r in resources if not is_nsfw(r)]
    if types:
        allowed = set(types)
        resources = [r for r in resources if r.pan_type in allowed]
    for res in resources:
        res.queries = list(dict.fromkeys([*res.queries, *equivalents]))
    score_all(resources, kw)
    picked = [r for r in resources if r.relevance >= EARLY_VERIFY_MIN_RELEVANCE]
    picked.sort(key=lambda r: (-r.relevance, -r.score))
    return picked


def _finish(outcome: SearchOutcome, alive_only: bool, strict: bool) -> None:
    resources = sort_resources(score_all(outcome.resources, outcome.keyword))
    if alive_only:
        kept = [r for r in resources if not confidently_irrelevant(r, outcome.keyword)]
        outcome.irrelevant_pruned += len(resources) - len(kept)
        resources, outcome.pruned = prune(kept, strict=strict)
    outcome.resources = resources


async def search(
    kw: str,
    *,
    types: list[PanType] | None = None,
    source_names: list[str] | None = None,
    do_verify: bool = True,
    alive_only: bool = False,
    strict: bool = False,
    relax: bool = True,
    sfw: bool = False,
    verify_budget: int | None = None,
    limit: int | None = None,
    on_progress: Callable[[SearchOutcome], Awaitable[None]] | None = None,
    cache: VerifyCache | None = None,
) -> SearchOutcome:
    started = time.monotonic()
    kw = kw.strip()
    # 发给各源的是"干净检索词"：用户常把标题原样粘进来（“三体”、"三体"、《三体》全集、三体!），
    # 标点留在检索词里会让每个源的子串/LIKE 匹配都 0 命中 —— 表现为"搜什么都搜不到"。
    # 展示与打分仍用用户原词（打分内部同样会做规范化）。
    query_kw = " ".join(split_query(kw)) or kw
    adapters = build_adapters(source_names)
    outcome = SearchOutcome(keyword=kw, used_sources=[a.name for a in adapters])
    outcome.queries_used = [query_kw]
    outcome.strict = strict

    if not kw:
        return outcome

    if not adapters:
        outcome.errors["_"] = "没有启用的数据源，请检查 config/sources.yaml"
        return outcome

    # ---- 查询级缓存：命中即返回整份结果（含验活状态），保证重复搜索一致 ----
    cache_ttl = cfg_float(sources_config(), "search_cache_ttl_seconds", 0.0)
    cache_key = None
    if cache_ttl > 0 and not limit:
        cache_key = _query_cache_key(kw, types, source_names, do_verify, alive_only,
                                     strict, relax, verify_budget, sfw)
        hit = _QUERY_CACHE.get(cache_key)
        if hit and time.monotonic() - hit[0] <= cache_ttl:
            cached = deepcopy(hit[1])
            cached.from_cache = True
            cached.timings = {"total": time.monotonic() - started}
            return cached

    # budget: 0 是文档化的"不限制（全量验活）"，不能被 or 吃掉
    budget = max(0, int(verify_budget if verify_budget is not None
                        else cfg_int(verify_cfg(), "budget", 300)))
    # 边抓边验只花一部分预算：先到的源（TG 本地索引）不能把预算吃光，
    # 慢源里更相关的结果还要验。
    share = cfg_float(verify_cfg(), "early_verify_share", 0.75)
    early_limit = int(budget * share) if budget > 0 else 0
    early_enabled = do_verify and share > 0 and (budget == 0 or early_limit > 0)

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            http2=True,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        ))
        pool = (await stack.enter_async_context(VerifierPool(cache=cache))
                if do_verify else None)
        prefetch = getattr(pool, "prefetch", None) if early_enabled else None
        peek = getattr(pool, "peek", None)

        _t_start = time.monotonic()
        # 主查询 / 补搜 / 别名替换 一起并行发出：
        #   补搜（relaxed=True）要降权 —— 它是放宽，召回多但精准度低
        #   别名（relaxed=False）不降权 —— 它是等价替换，命中结果按替换后的词打分
        plan: list[tuple[str, bool]] = [(query_kw, False)]
        if relax:
            plan += [(q, True) for q in relaxed_queries(kw)]
            plan += [(q, False) for q in alias_queries(kw)]
        plan = list(dict.fromkeys(plan))
        equivalents = [q for q, relaxed in plan if not relaxed]
        jobs = []
        hits: list[RawHit] = []
        completed = {}
        throttle = PreviewThrottle(
            cfg_float(sources_config(), "preview_min_interval_seconds",
                      PREVIEW_MIN_INTERVAL_SECONDS),
            cfg_int(sources_config(), "preview_min_new_hits", PREVIEW_MIN_NEW_HITS),
        )

        async def fetch_job(number, adapter, query, relaxed):
            batch = await _fetch_hits([adapter], client, query)
            return number, query, relaxed, batch

        for q, is_relaxed in plan:
            for adapter in adapters:
                if adapter.primary_only and is_relaxed:
                    continue
                jobs.append(asyncio.create_task(fetch_job(len(jobs), adapter, q, is_relaxed)))
        try:
            for task in asyncio.as_completed(jobs):
                number, query, is_relaxed, (got, errs, rep) = await task
                completed[number] = [h.model_copy(update={"relaxed": is_relaxed, "query": query})
                                     for h in got]
                for name, err in errs.items():
                    outcome.errors.setdefault(name, err)
                _merge_report(outcome.source_report, rep)
                outcome.degraded = sorted(name for name, report in outcome.source_report.items()
                                          if report["status"] in ("error", "timeout", "degraded"))
                hits = [h for n in sorted(completed) for h in completed[n]]
                if sfw:
                    hits, outcome.adult_pruned = filter_adult_hits(hits)
                outcome.queries_used = list(dict.fromkeys([query_kw, *[h.query for h in hits if h.query]]))
                if prefetch and got:
                    batch = completed[number]
                    if sfw:
                        batch, _ = filter_adult_hits(batch)
                    candidates = await asyncio.to_thread(
                        _early_candidates, batch, kw, equivalents, types, sfw)
                    prefetch(candidates, limit=early_limit)
                if on_progress and got:
                    now = time.monotonic()
                    if not throttle.should_emit(now, len(hits)):
                        # 节流掉的那几帧不是"丢结果"：完整结果由收尾的
                        # _prepare + on_progress("complete") 给出。
                        throttle.skipped += 1
                    else:
                        throttle.mark(now, len(hits))
                        # 浅拷贝就够：resources 紧接着被 _prepare 整个替换，没必要把上一帧
                        # 那几百条 Resource 深拷一遍（那是每次预览的固定大开销）。
                        preview = copy(outcome)
                        # CPU 工作移出事件循环，慢源继续并发运行。
                        await asyncio.to_thread(_prepare, preview, hits, equivalents,
                                                types, alive_only, sfw)
                        if peek:
                            # 已经验完的直接带上状态：预览里就能看到"有效"，死链不再露面
                            for res in preview.resources:
                                if res.verify is None and (known := peek(res)) is not None:
                                    res.verify = known
                        preview.resources = sort_resources(preview.resources)
                        if alive_only:
                            preview.resources, _ = prune(preview.resources, strict=strict)
                        preview.timings = {"elapsed": time.monotonic() - started}
                        await on_progress(preview)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        outcome.timings["fetch"] = time.monotonic() - _t_start
        prep_start = time.monotonic()
        # sfw 也要传：命中级过滤（adult_sources）在上面的循环里做了，这里还有一层
        # 资源级兜底（nsfw_sources，按去重后的 sources/kinds 判）。两处必须一致，
        # 否则最后一帧预览和最终结果会不一样。
        await asyncio.to_thread(_prepare, outcome, hits, equivalents, types, alive_only, sfw)
        outcome.timings["prepare"] = time.monotonic() - prep_start
        resources = outcome.resources
        verify_start = time.monotonic()

        if pool is not None and resources:
            # ---- 验活预算：先按相关性排序，只验活最相关的前 N 条 ----
            # 大结果集（700+）全量验活要 1~2 分钟，而用户只看前几十条。
            # 边抓边验已发起的部分计入同一预算，这里只补验剩余名额并等它们收尾。
            resources.sort(key=lambda r: (-r.relevance, -r.score))
            await pool.verify_all(resources, budget=budget)
            outcome.verify_stats = dict(pool.stats)
            outcome.verify_budget_skipped = pool.stats.get("budget_skipped", 0)
            outcome.verify_timeout_skipped = pool.stats.get("timeout_skipped", 0)
        outcome.timings["verify"] = time.monotonic() - verify_start if do_verify and resources else 0.0

    final_start = time.monotonic()
    await asyncio.to_thread(_finish, outcome, alive_only, strict)
    resources = outcome.resources
    outcome.timings["rank"] = time.monotonic() - final_start
    outcome.timings["total"] = time.monotonic() - started

    if limit:
        resources = resources[:limit]

    outcome.resources = resources

    # 只在"有结果"或"没有源降级"时缓存 —— 一次源故障导致的空结果必须允许重试，
    # 不能被缓存成"确实没有"。
    if cache_key and (resources or not outcome.degraded):
        _QUERY_CACHE[cache_key] = (time.monotonic(), deepcopy(outcome))
        if len(_QUERY_CACHE) > _QUERY_CACHE_MAX:
            oldest = min(_QUERY_CACHE, key=lambda k: _QUERY_CACHE[k][0])
            _QUERY_CACHE.pop(oldest, None)
    return outcome
