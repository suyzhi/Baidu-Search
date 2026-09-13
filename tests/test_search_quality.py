"""召回、误删、预算与截止时间的跨模块回归测试；全部使用隔离索引/模拟网络。"""

import asyncio

import httpx
import pytest

from pansearch import pipeline
from pansearch.adapters.base import Adapter, publish_hits
from pansearch.adapters.telegram import TelegramAdapter
from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status, VerifyResult
from pansearch.score import confidently_irrelevant, score_all, sort_resources
from pansearch.store import VerifyCache
from pansearch.tgindex import TgIndex, TgMessage
from pansearch.verifiers import VerifierPool


def hit(token, title=None, **kw):
    return RawHit(source="test", kind="pansou", url=f"https://pan.quark.cn/s/{token}",
                  title=title, **kw)


def test_exact_unchecked_beats_verified_noise_and_partial():
    good, partial, noise = build_resources([
        hit("good", "machine learning"), hit("partial", "War Machine"), hit("noise", "随机广告"),
    ])
    for res in [partial, noise]:
        res.verify = VerifyResult(status=Status.ALIVE)
    assert sort_resources(score_all([noise, partial, good], "machine learning"))[0] is good


def test_duplicate_title_does_not_destroy_good_match():
    resources = build_resources([hit("same", "沙丘 4K HDR"), hit("same", "欢迎关注频道，每天分享热门电影电视剧")])
    score_all(resources, "沙丘 4K HDR")
    assert resources[0].title == "沙丘 4K HDR"
    assert not confidently_irrelevant(resources[0], "沙丘 4K HDR")


def test_full_match_from_relax_is_not_penalized_and_repetition_has_no_bonus():
    primary = build_resources([hit("one", "沙丘 4K HDR")])[0]
    relaxed = build_resources([hit("two", "沙丘 4K HDR", relaxed=True)] * 10)[0]
    score_all([primary, relaxed], "沙丘 4K HDR")
    assert primary.score == relaxed.score


@pytest.mark.parametrize("title,query,drop", [
    (None, "沙丘", False), ("全集", "沙丘", False),
    ("Dune Part Two", "沙丘", False),
    ("沙丘 2 4K", "沙丘2", False),
    ("Omnisphere Factory Library", "大气合成器", False),
    ("沙丘 1080p", "4K 沙丘 HDR", False),
    ("黑夏 4K HDR", "4K 沙丘 HDR", True),
    ("Video Codec", "code", True),
    # 锚点是 IDF 最高的内容词（"learning" 在语料里比 "machine" 稀有）；
    # 只命中 "machine" 的 "War Machine" 缺少锚点 -> 判为无关。
    ("War Machine", "machine learning", True),
])
def test_conservative_relevance_filter(title, query, drop):
    res = build_resources([hit("test", title)])[0]
    res.queries = [query, *pipeline.alias_queries(query)]
    assert confidently_irrelevant(res, query) is drop


def test_nfkc_and_sequel_boundary():
    exact, year = build_resources([hit("good", "沙丘 ２ ４Ｋ"), hit("year", "沙丘 2024 4K")])
    score_all([exact, year], "沙丘 2 4K")
    assert exact.relevance == 1
    assert year.relevance < 0.85


def test_compact_sequel_matches_spaces_without_matching_year():
    exact, year = build_resources([hit("good", "沙丘 2 4K"), hit("year", "沙丘2024 4K")])
    score_all([exact, year], "沙丘2")
    assert exact.relevance >= 0.95
    assert year.relevance < 0.45


def test_default_sites_use_catalog_routing():
    from pansearch.config import source_cfg
    from pansearch.adapters.sitesearch import SiteSearchAdapter
    cfg = dict(source_cfg("sitesearch"), health_tracking=False)
    assert not cfg.get("sites")
    sites = SiteSearchAdapter(cfg).select_sites("沙丘 4K HDR")
    assert all(set(s.verticals) & {"movie", "general"} for s in sites)


def test_query_echo_is_not_evidence_of_matching_quality():
    from pansearch.adapters.pansou import _clean_note
    query = "沙丘 4K HDR"
    assert _clean_note("沙丘 4K HDR 沙丘 1080P", query) == "沙丘 1080P"
    assert _clean_note("沙丘 4K HDR 蓝光原盘", query) == "沙丘 4K HDR 蓝光原盘"
    res = build_resources([hit("real", "沙丘2 4kHDR")])[0]
    score_all([res], query)
    assert res.relevance == 1.0


class PartialSource(Adapter):
    name = "partial"

    async def search(self, kw, client):
        publish_hits([hit(kw, kw)])
        await asyncio.Event().wait()


async def test_deadline_keeps_partial_hits_isolated_by_query():
    source = PartialSource({"deadline": 0.02})
    batches = await asyncio.gather(*(pipeline._fetch_hits([source], None, q) for q in ["one", "two"]))
    for query, (hits, errors, _report) in zip(["one", "two"], batches):
        assert [h.title for h in hits] == [query]
        assert "保留" in errors["partial"]


async def test_primary_only_source_is_not_reenabled_when_alone(monkeypatch):
    class Source(Adapter):
        name = "primary"
        primary_only = True
        calls = []

        async def search(self, kw, client):
            self.calls.append(kw)
            return []

    source = Source()
    monkeypatch.setattr(pipeline, "build_adapters", lambda _: [source])
    await pipeline.search("沙丘 4K HDR", do_verify=False)
    assert source.calls == ["沙丘 4K HDR"]


async def test_default_filter_keeps_unknown_good_links_and_removes_noise(monkeypatch):
    class Source(Adapter):
        name = "test"

        async def search(self, kw, client):
            return [hit("good", "沙丘 4K HDR"), hit("bad", "黑夏 4K HDR"), hit("unknown")]

    monkeypatch.setattr(pipeline, "build_adapters", lambda _: [Source()])
    out = await pipeline.search("沙丘 4K HDR", alive_only=True, do_verify=False, relax=False)
    assert {r.url.rsplit("/", 1)[-1] for r in out.resources} == {"good", "unknown"}
    assert out.irrelevant_pruned == 1
    all_out = await pipeline.search("沙丘 4K HDR", alive_only=False, do_verify=False, relax=False)
    assert len(all_out.resources) == 3


def message(i, text, links=True):
    return TgMessage("test", i, "2026-09-01T00:00:00Z", text,
                     [{"url": f"https://pan.quark.cn/s/test{i}"}] if links else [])


def test_index_does_not_spend_limit_on_linkless_messages_or_qualifiers(tmp_path):
    index = TgIndex(tmp_path / "tg.sqlite3")
    try:
        index.upsert([message(i, "沙丘 4K HDR", False) for i in range(10, 30)] +
                     [message(2, "沙丘 1080p"), message(3, "黑夏 4K HDR")])
        rows = index.search("沙丘 4K HDR", limit=1)
        assert [r["msg_id"] for r in rows] == [2]
    finally:
        index.close()


def test_index_treats_like_wildcard_literally(tmp_path):
    index = TgIndex(tmp_path / "tg.sqlite3")
    try:
        index.upsert([message(1, "100%完成"), message(2, "1000完成")])
        assert len(index.search("100%")) == 1
    finally:
        index.close()


async def test_index_miss_does_not_recrawl_populated_index(tmp_path, monkeypatch):
    path = tmp_path / "tg.sqlite3"
    index = TgIndex(path)
    index.upsert([message(1, "沙丘")])
    index.close()
    adapter = TelegramAdapter({"index_path": str(path)})

    async def fail():
        pytest.fail("关键词未命中不能触发全频道重抓")

    monkeypatch.setattr(adapter, "_seed", fail)
    assert await adapter.search("unfindable", None) == []


def test_long_message_keeps_keyword_context():
    row = {"channel": "test", "msg_id": 1, "text": "频道介绍 " * 120 + "沙丘 4K HDR",
           "links": [{"url": "https://pan.quark.cn/s/long"}]}
    assert "沙丘" in TelegramAdapter._to_hits([row], "沙丘")[0].title


async def test_cache_and_unsupported_do_not_consume_network_budget(tmp_path):
    cache = VerifyCache(tmp_path / "verify.sqlite3")
    resources = build_resources([hit("cached", "沙丘"), hit("dead", "沙丘"), hit("new", "沙丘"),
        RawHit(source="s", kind="tg", url="https://pan.xunlei.com/s/unsupported", title="沙丘"),
        hit("skip", "沙丘")])
    cache.put(resources[0].key, VerifyResult(status=Status.ALIVE, title_hint="真实标题"))
    cache.put(resources[1].key, VerifyResult(status=Status.DEAD))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"stoken": "token"}})

    try:
        async with VerifierPool(cfg={"retries": 0}, cache=cache) as pool:
            await pool._client.aclose()
            pool._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await pool.verify_all(resources, budget=1)
            assert pool.stats["cache_hit"] == 2
            assert pool.stats["budget_skipped"] == 1
        assert len(requests) == 1
        assert resources[2].status is Status.ALIVE
        assert resources[3].status is Status.UNSUPPORTED
        assert resources[4].status is Status.UNCHECKED
    finally:
        cache.close()


async def test_verify_deadline_preserves_completed_results_and_cancels_requests(tmp_path):
    cache = VerifyCache(tmp_path / "verify.sqlite3")
    resources = build_resources([hit("fast", "沙丘"), hit("slow", "沙丘")])
    cancelled = asyncio.Event()

    async def handler(request):
        if b'"slow"' in request.content:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200, json={"code": 0, "data": {"stoken": "token"}})

    try:
        async with VerifierPool(cfg={"deadline": 0.05, "services": {"quark": {"qps": 10000}}}, cache=cache) as pool:
            await pool._client.aclose()
            pool._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await asyncio.wait_for(pool.verify_all(resources, budget=2), timeout=1)
            assert pool.stats["timeout_skipped"] == 1
        assert cancelled.is_set()
        assert resources[0].status is Status.ALIVE
        assert resources[1].status is Status.UNCHECKED
    finally:
        cache.close()


async def test_cache_restores_title_and_password_from_url(tmp_path):
    cache = VerifyCache(tmp_path / "verify.sqlite3")
    resource = build_resources([hit("cold")])[0]
    cache.put(resource.key, VerifyResult(status=Status.ALIVE, title_hint="沙丘"))
    try:
        async with VerifierPool(cache=cache) as pool:
            await pool.verify_all([resource], budget=1)
        assert resource.title == "沙丘"
        assert pool.stats["cache_hit"] == 1
    finally:
        cache.close()
    baidu = build_resources([RawHit(source="s", kind="tg", url="https://pan.baidu.com/s/1abc?pwd=1234")])[0]
    assert baidu.pwd == "1234"
