"""搜索编排测试：放宽查询、验活预算、单源超时隔离。"""

from __future__ import annotations

import asyncio

import pytest

from pansearch import pipeline
from pansearch.adapters.base import Adapter
from pansearch.models import Status, VerifyResult
from pansearch.pipeline import _fetch_hits, relaxed_queries, search


# ---------------------------------------------------------------- 放宽查询
@pytest.mark.parametrize(
    "kw,expected",
    [
        # 实测「沙丘 4K HDR」在聚合引擎只有个位数~几十条，「沙丘」有 180+ 条
        ("沙丘 4K HDR", ["沙丘", "4K HDR"]),
        ("三体 全集", ["三体", "全集"]),
        ("Dune 4K", ["Dune", "4K"]),
        ("沙丘、4K、HDR", ["沙丘", "4K HDR"]),
    ],
)
def test_relaxed_queries_multiword(kw, expected):
    assert relaxed_queries(kw) == expected


@pytest.mark.parametrize(
    "kw,expected",
    [
        # 实测「沙丘 2」曾拆出补搜词 "2"，而索引里 18.4 万条消息含 "2"（占 87%）
        ("沙丘 2", ["沙丘"]),
        ("沙丘 2024", ["沙丘"]),
        ("沙丘、2", ["沙丘"]),
        ("阿凡达 2 4K", ["阿凡达", "4K"]),
        ("2 沙丘", ["沙丘"]),          # 主词无区分度时，只能靠限定词
        ("1 2", []),                  # 全是无区分度的词 -> 不补搜
    ],
)
def test_relaxed_queries_filters_useless_terms(kw, expected):
    assert relaxed_queries(kw) == expected


@pytest.mark.parametrize("kw", ["沙丘", "三体", "", "  ", "2", "2024"])
def test_relaxed_queries_single_word_is_noop(kw):
    assert relaxed_queries(kw) == []


def test_relaxed_queries_never_repeats_original():
    assert "沙丘 4K HDR" not in relaxed_queries("沙丘 4K HDR")


async def test_useless_relaxed_term_is_not_queried(monkeypatch):
    """「沙丘 2」不该再去补搜 "2"（那是全库扫描，白等几十秒）。"""
    adapter = StubAdapter({}, _quark_hits(2))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    await search("沙丘 2", do_verify=True, alive_only=False, relax=True, verify_budget=0)
    assert adapter.calls == ["沙丘 2", "沙丘"]


# ---------------------------------------------------------------- 桩
class StubAdapter(Adapter):
    name = "stub"
    kind = "pansou"

    def __init__(self, cfg=None, hits=None, delay: float = 0.0):
        super().__init__(cfg or {})
        self._hits = hits or []
        self._delay = delay
        self.calls: list[str] = []

    async def search(self, kw, client):
        self.calls.append(kw)
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._hits)


class StubPool:
    """替掉真实的 VerifierPool，避免测试打网络。"""

    instances: list["StubPool"] = []

    def __init__(self, *a, **kw):
        self.stats = {"checked": 0, "cache_hit": 0, "alive": 0, "dead": 0,
                      "error": 0, "need_pwd": 0, "wrong_pwd": 0,
                      "unsupported": 0, "pruned": 0, "by_service": {}}
        self.seen = 0
        StubPool.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def verify_all(self, resources):
        self.seen = len(resources)
        for res in resources:
            res.verify = VerifyResult(status=Status.ALIVE, method="stub")
        self.stats["checked"] = len(resources)
        self.stats["alive"] = len(resources)


def _quark_hits(n: int, prefix: str = "沙丘") -> list:
    from pansearch.models import RawHit

    return [
        RawHit(source="stub", kind="pansou",
               url=f"https://pan.quark.cn/s/{prefix}{i:012d}", title=f"{prefix} 第{i}部")
        for i in range(n)
    ]


# ---------------------------------------------------------------- 验活预算
async def test_verify_budget_limits_verification(monkeypatch):
    adapter = StubAdapter({}, _quark_hits(40))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)
    StubPool.instances.clear()

    out = await search("沙丘", do_verify=True, alive_only=False,
                       relax=False, verify_budget=10)

    assert StubPool.instances[0].seen == 10, "只应验活预算内的 10 条"
    assert out.verify_budget_skipped == 30
    unchecked = [r for r in out.resources if r.status is Status.UNCHECKED]
    assert len(unchecked) == 30
    assert all("验活预算" in (r.verify.note or "") for r in unchecked)


async def test_verify_budget_zero_means_unlimited(monkeypatch):
    adapter = StubAdapter({}, _quark_hits(25))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)
    StubPool.instances.clear()

    out = await search("沙丘", do_verify=True, alive_only=False,
                       relax=False, verify_budget=0)
    assert StubPool.instances[0].seen == 25
    assert out.verify_budget_skipped == 0


async def test_budget_keeps_most_relevant(monkeypatch):
    """预算按相关性截断，被丢掉的必须是相关性最低的那批。"""
    from pansearch.models import RawHit

    hits = [
        RawHit(source="s", kind="pansou", url="https://pan.quark.cn/s/relevant0001",
               title="沙丘 4K HDR 全集"),
        *[RawHit(source="s", kind="pansou", url=f"https://pan.quark.cn/s/noise{i:07d}",
                 title=f"完全无关的东西 {i}") for i in range(20)],
    ]
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [StubAdapter({}, hits)])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)
    StubPool.instances.clear()

    out = await search("沙丘 4K HDR", do_verify=True, alive_only=False,
                       relax=False, verify_budget=1)
    checked = [r for r in out.resources if r.status is Status.ALIVE]
    assert len(checked) == 1
    assert "沙丘" in checked[0].title


# ---------------------------------------------------------------- 单源超时
async def test_slow_source_is_skipped_not_fatal(monkeypatch):
    slow = StubAdapter({"deadline": 0.05}, delay=5.0)
    fast = StubAdapter({"deadline": 5.0}, _quark_hits(2))

    hits, errors = await _fetch_hits([slow, fast], None, "沙丘")

    assert len(hits) == 2, "快源的结果必须保留"
    assert "超时" in errors["stub"] or errors, "慢源必须被记录为超时"


async def test_source_error_is_isolated(monkeypatch):
    class Boom(StubAdapter):
        async def search(self, kw, client):
            raise RuntimeError("站点挂了")

    hits, errors = await _fetch_hits([Boom({}), StubAdapter({}, _quark_hits(3))], None, "沙丘")
    assert len(hits) == 3
    assert "站点挂了" in errors["stub"]


# ---------------------------------------------------------------- 并行放宽
async def test_relaxed_queries_are_all_requested(monkeypatch):
    adapter = StubAdapter({}, _quark_hits(2))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    await search("沙丘 4K HDR", do_verify=True, alive_only=False, relax=True, verify_budget=0)
    assert adapter.calls == ["沙丘 4K HDR", "沙丘", "4K HDR"]


async def test_relax_disabled_uses_single_query(monkeypatch):
    adapter = StubAdapter({}, _quark_hits(2))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    out = await search("沙丘 4K HDR", do_verify=True, alive_only=False, relax=False, verify_budget=0)
    assert adapter.calls == ["沙丘 4K HDR"]
    assert out.queries_used == ["沙丘 4K HDR"]


# ---------------------------------------------------------------- 中文别名
def test_alias_queries_expands_chinese_nicknames():
    """回归：实测「大气合成器」原始命中 0 条，而同义的 "Omnisphere" 有 20+ 条。"""
    from pansearch.pipeline import alias_queries

    assert alias_queries("大气合成器") == ["omnisphere"]
    assert alias_queries("血清") == ["serum"]
    assert alias_queries("康泰克") == ["kontakt"]


def test_alias_queries_keeps_qualifiers():
    from pansearch.pipeline import alias_queries

    assert alias_queries("大气合成器 4K") == ["omnisphere 4k"]


def test_alias_queries_noop_for_unknown_and_english():
    from pansearch.pipeline import alias_queries

    assert alias_queries("Serum") == []
    assert alias_queries("完全没收录的词") == []
    assert alias_queries("") == []


async def test_alias_hits_are_not_downweighted(monkeypatch):
    """别名是等价替换，不是放宽 —— 命中结果不该被 relaxed_penalty 降权。"""
    adapter = StubAdapter({}, _quark_hits(2, prefix="Omnisphere"))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    out = await search("大气合成器", do_verify=True, alive_only=False,
                       relax=True, verify_budget=0)
    assert "omnisphere" in adapter.calls
    assert all(r.from_primary for r in out.resources), "别名命中不该被当成补搜降权"


async def test_alias_hits_scored_against_alias_word(monkeypatch):
    """否则会自相矛盾：用别名取回一堆 "Omnisphere" 结果，再用「大气合成器」算相关性 -> 全 0.1 分。"""
    from pansearch.score import score_resource
    from pansearch.config import scoring_cfg
    from pansearch.models import RawHit

    adapter = StubAdapter({}, [
        RawHit(source="s", kind="pansou", url="https://pan.quark.cn/s/omnisphere01",
               title="Omnisphere 2 Factory Library", query="omnisphere"),
    ])
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    out = await search("大气合成器", do_verify=True, alive_only=False,
                       relax=True, verify_budget=0)
    res = out.resources[0]
    assert res.queries == ["omnisphere"]
    assert score_resource(res, "大气合成器", scoring_cfg()) > 0.5


# ---------------------------------------------------------------- 慢源只跑主查询
class SlowStub(StubAdapter):
    """模拟 sitesearch：慢，且只该对主查询/别名查询运行。"""
    primary_only = True


async def test_primary_only_source_skips_relaxed_queries(monkeypatch):
    """回归：sitesearch 每个查询要 5~10 秒，跟着补搜词再跑一遍是纯浪费
    （实测补搜词 "模板" 只回 8 条却要 6 秒）。"""
    slow = SlowStub({}, _quark_hits(3))
    fast = StubAdapter({}, _quark_hits(3))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [slow, fast])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    await search("AE 模板", do_verify=True, alive_only=False, relax=True, verify_budget=0)

    assert slow.calls == ["AE 模板"], "慢源只该跑主查询"
    assert fast.calls == ["AE 模板", "AE", "模板"], "快源照常跑补搜"


async def test_primary_only_source_still_runs_for_alias(monkeypatch):
    """别名是等价替换（不是放宽），慢源也要跟着跑。"""
    slow = SlowStub({}, _quark_hits(3, prefix="Omnisphere"))
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [slow])
    monkeypatch.setattr(pipeline, "VerifierPool", StubPool)

    await search("大气合成器", do_verify=True, alive_only=False, relax=True, verify_budget=0)
    assert slow.calls == ["大气合成器", "omnisphere"]
