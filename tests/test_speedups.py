"""2026-09 性能/召回改动的回归测试（全部离线）。

锁住的实测结论（473 万条索引）：
  * 大候选集不做全量 bm25（「4K」4.5 s → 17 ms）
  * 单个拉丁字母走 FTS，不回退全表 LIKE（「C# 教程」48 s → 0.36 s）
  * 死链缓存 30 天；边抓边验的结果收尾时复用、计入同一预算
  * 抓最新追平已有位置即停；深挖按产出率分页、跳过已到底的频道
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from pansearch import pipeline, textindex, tgindex
from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status, VerifyResult
from pansearch.store import VerifyCache
from pansearch.tgindex import TgCrawler, TgIndex, TgMessage, deepen_pages
from pansearch.verifiers import VerifierPool


def _msg(i, text, links=True, channel="a", posted=None):
    return TgMessage(channel, i, posted, text,
                     [{"url": f"https://pan.quark.cn/s/x{i:010d}"}] if links else [])


# ---------------------------------------------------------------- TG 检索
def test_single_latin_letter_uses_fts_not_like(tmp_path, monkeypatch):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "C# 入门教程"), _msg(2, "C++ 教程"), _msg(3, "C语言 教程"),
                    _msg(4, "维生素C 说明")])
        monkeypatch.setattr(idx, "_search_like", lambda *a: pytest.fail("不应回退 LIKE"))
        assert textindex.match_phrase("c#") == '"c"'
        assert [r["msg_id"] for r in idx.search("C# 教程")] == [1]
        assert [r["msg_id"] for r in idx.search("C++")] == [2]
    finally:
        idx.close()


def test_single_cjk_char_uses_prefix_and_recheck(tmp_path, monkeypatch):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "电子书 合集"), _msg(2, "书法 字帖"), _msg(3, "完全无关"),
                    _msg(4, "好书")])
        monkeypatch.setattr(idx, "_search_like", lambda *a: pytest.fail("不应回退 LIKE"))
        # 前缀匹配以「书」开头的 bigram（「书合」「书法」）；
        # 「书」恰在整段中文末尾时（「好书」）召回不到 —— 已知取舍，换来不扫全表
        assert {r["msg_id"] for r in idx.search("书")} == {1, 2}
    finally:
        idx.close()


def test_large_candidate_set_skips_bm25_and_keeps_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(tgindex, "RANK_MAX_CANDIDATES", 1)
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([
            _msg(1, "沙丘 纪录片", posted="2026-01-01"),
            _msg(2, "沙丘 4K HDR", posted="2025-01-01"),
            _msg(3, "沙丘 花絮", posted="2026-02-01"),
            _msg(4, "无关 4K", posted="2026-03-01"),
        ])
        rows = idx.search("沙丘 4K", limit=10)
        # 第 1 层（全部词）在前；同层内按时间新→旧；不含主题词的不召回
        assert [r["msg_id"] for r in rows] == [2, 3, 1]
    finally:
        idx.close()


def test_multi_subject_and_first_then_or_fill(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "机器 学习 入门"), _msg(2, "机器 人 大战"), _msg(3, "深度 学习")])
        rows = idx.search("机器 学习", limit=10)
        assert rows[0]["msg_id"] == 1                 # 两个主题词都在的优先
        assert {r["msg_id"] for r in rows} == {1, 2, 3}
        assert rows[0]["hits"] > rows[-1]["hits"]
        assert [r["msg_id"] for r in idx.search("机器 学习", limit=1)] == [1]
    finally:
        idx.close()


def test_or_tier_keeps_cjk_rows_when_latin_subject_rechecked(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "Kontakt 采样器"), _msg(2, "Kontaktology 专辑"), _msg(3, "只有 采样器")])
        ids = {r["msg_id"] for r in idx.search("Kontakt 采样器", limit=10)}
        assert 1 in ids and 3 in ids and 2 not in ids
    finally:
        idx.close()


def test_short_latin_terms_match_whole_words_only(tmp_path):
    """「FL Studio」：fl* 会展开成 flac/flv，在大索引上把复核预算吃光（回归：0 条）。"""
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "FL Studio 20 教程"), _msg(2, "FLAC 无损 录音")])
        assert [r["msg_id"] for r in idx.search("FL")] == [1]
    finally:
        idx.close()


def test_recheck_budget_ignores_sql_time(tmp_path, monkeypatch):
    monkeypatch.setattr(tgindex, "RECHECK_BUDGET_SECONDS", 0.0)
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(1, "FL Studio 20 教程")])
        # 预算为 0 也要交出第一条通过复核的结果，而不是因为 SQL 慢一条都不给
        assert [r["msg_id"] for r in idx.search("FL Studio 破解")] == [1]
    finally:
        idx.close()


def test_index_uses_wal(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        assert idx.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        idx.close()


def test_compact_clears_only_linkless_text(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        # 默认照存无链接正文：频道反查（t.me/xxx 提及）靠它
        idx.upsert([_msg(1, "关注 t.me/somechannel", links=False), _msg(2, "沙丘")])
        assert idx.compact()["cleared"] == 1
        texts = dict(idx.conn.execute("SELECT msg_id, text FROM tg_messages"))
        assert texts == {1: "", 2: "沙丘"}
        assert idx.stats()["messages"] == 2           # 行保留：翻页位置与统计不变
    finally:
        idx.close()


# ---------------------------------------------------------------- 抓取调度
def test_deepen_pages_by_yield():
    assert deepen_pages(100, 50, 0) == 100            # 样本太少：满额
    assert deepen_pages(100, 5000, None) == 100       # 旧库未统计：满额
    assert deepen_pages(100, 5000, 2000) == 100
    assert deepen_pages(100, 5000, 1000) == 50
    assert deepen_pages(100, 5000, 200) == 20
    assert deepen_pages(100, 5000, 10) == 1


class _FakeCrawler(TgCrawler):
    def __init__(self, pages_by_before, **kw):
        super().__init__(pages_delay=0, **kw)
        self.pages_by_before = pages_by_before
        self.requested = []

    async def _page(self, client, channel, before):
        self.requested.append(before)
        msgs = self.pages_by_before.get(before, [])
        return msgs, "ok" if msgs else "empty"


async def test_latest_crawl_stops_at_known_newest(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(10, "旧", channel="ch")])
        idx.mark_channel("ch", newest=10, oldest=10)
        crawler = _FakeCrawler({None: [_msg(i, "新", channel="ch") for i in (11, 12, 13)],
                                11: [_msg(i, "更旧", channel="ch") for i in (8, 9, 10)],
                                8: [_msg(i, "更更旧", channel="ch") for i in (5, 6, 7)]},
                               index=idx)
        await crawler.crawl_channel(None, "ch", pages=10)
        assert crawler.requested == [None, 11]        # 第二页已经翻到 10，追平即停
        assert crawler.stats["caught_up"] == 1
    finally:
        idx.close()


async def test_deepen_marks_exhausted_and_skips_next_time(tmp_path):
    idx = TgIndex(tmp_path / "tg.sqlite3")
    try:
        idx.upsert([_msg(5, "x", channel="ch")])
        idx.mark_channel("ch", newest=5, oldest=5)
        crawler = _FakeCrawler({}, index=idx)          # before=5 之前没有消息了
        await crawler.crawl(["ch"], pages=3, deepen=True)
        assert idx.channel_yield()["ch"][2] is True

        again = _FakeCrawler({}, index=idx)
        stats = await again.crawl(["ch"], pages=3, deepen=True)
        assert again.requested == [] and stats["skipped_exhausted"] == 1
    finally:
        idx.close()


# ---------------------------------------------------------------- 验活缓存
def test_dead_links_cached_longer_than_alive(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=0.0001, dead_ttl_hours=1)
    cache.put("dead1", VerifyResult(status=Status.DEAD))
    cache.put("gone1", VerifyResult(status=Status.NOT_FOUND))
    cache.put("live1", VerifyResult(status=Status.ALIVE))
    time.sleep(0.5)
    assert cache.get("live1") is None
    assert cache.get("dead1").status is Status.DEAD
    assert cache.get("gone1").status is Status.NOT_FOUND
    cache.close()


def test_zero_ttl_means_never_expire_for_dead_too(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=0, dead_ttl_hours=1)
    assert cache.dead_ttl == 0
    cache.close()


# ---------------------------------------------------------------- 边抓边验
def _quark(i, pwd=None):
    return build_resources([RawHit(source="t", kind="t", url=f"https://pan.quark.cn/s/q{i:010d}",
                                   pwd=pwd, title="沙丘")])[0]


async def _pool(tmp_path, handler, **cfg):
    pool = VerifierPool(cfg={"retries": 0, "concurrency": 4, "timeout": 5, **cfg},
                        cache=VerifyCache(tmp_path / "c.sqlite3"))
    await pool.__aenter__()
    await pool._client.aclose()
    pool._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return pool


async def test_prefetch_results_reused_and_counted_in_budget(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"status": 200, "code": 0})

    pool = await _pool(tmp_path, handler)
    try:
        early = [_quark(i) for i in range(3)]
        assert pool.prefetch(early, limit=2) == 2      # 提前验活上限
        await asyncio.sleep(0.05)
        assert pool.peek(_quark(0)).status is Status.ALIVE
        assert pool.peek(_quark(2)) is None

        final = [_quark(i) for i in range(5)]          # 收尾时是重新构建的对象
        await pool.verify_all(final, budget=3)
        statuses = [r.status for r in final]
        assert statuses[:3] == [Status.ALIVE] * 3      # 2 条复用 + 1 条补验
        assert statuses[3:] == [Status.UNCHECKED] * 2  # 预算 3 已用完
        assert len(calls) == 3                         # 提前验过的没有重复请求
        assert pool.stats["early"] == 2
    finally:
        await pool.__aexit__()


async def test_prefetch_key_includes_password(tmp_path):
    pool = await _pool(tmp_path, lambda r: httpx.Response(200, json={"status": 200, "code": 0}))
    try:
        pool.prefetch([_quark(1)], limit=0)
        await asyncio.sleep(0.05)
        assert pool.peek(_quark(1, pwd="abcd")) is None   # 带码的是另一次校验
    finally:
        await pool.__aexit__()


async def test_unfinished_prefetch_times_out_as_unchecked(tmp_path):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"status": 200, "code": 0})

    pool = await _pool(tmp_path, slow, deadline=0.1)
    try:
        pool.prefetch([_quark(1)])
        final = [_quark(1)]
        await pool.verify_all(final, budget=5)
        assert final[0].status is Status.UNCHECKED
        assert pool.stats["timeout_skipped"] == 1
    finally:
        await pool.__aexit__()


async def test_pipeline_prefetches_while_slow_source_runs(monkeypatch):
    from pansearch.adapters.base import Adapter

    release = asyncio.Event()
    prefetched = []

    class Fast(Adapter):
        name = "fast"
        async def search(self, kw, client):
            return [RawHit(source="fast", kind="pansou", url="https://pan.quark.cn/s/fast00000001",
                           title="沙丘"),
                    RawHit(source="fast", kind="pansou", url="https://pan.quark.cn/s/noise0000001",
                           title="完全无关")]

    class Slow(Adapter):
        name = "slow"
        async def search(self, kw, client):
            await release.wait()
            return []

    class Pool:
        def __init__(self, *a, **kw):
            self.stats = {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        def prefetch(self, resources, *, limit=0):
            prefetched.extend(r.key for r in resources)
            release.set()                  # 慢源还没返回时就已经开始验活
            return len(resources)
        def peek(self, res):
            return None
        async def verify_all(self, resources, *, budget=0):
            for r in resources:
                r.verify = VerifyResult(status=Status.ALIVE)

    monkeypatch.setattr(pipeline, "build_adapters", lambda _: [Fast(), Slow()])
    monkeypatch.setattr(pipeline, "VerifierPool", Pool)
    out = await asyncio.wait_for(pipeline.search("沙丘", relax=False, verify_budget=10), 2)
    assert prefetched == ["quark:pan.quark.cn/s/fast00000001"]   # 只送高相关的
    assert out.resources


# ---------------------------------------------------------------- TG 标题
@pytest.mark.parametrize("segment,filler", [
    ("?pwd=8888", True), ("夸克", True), ("UC", True), ("?pwd=6742 夸克", True),
    ("百度网盘 提取码: abcd", True), ("🔗 下载地址", True), ("115", True),
    ("Xcode 15", False), ("Lucky", False), ("沙丘2", False), ("沙丘2 提取码 abcd", False),
])
def test_filler_segments_are_not_titles(segment, filler):
    from pansearch.adapters.telegram import _is_filler

    assert _is_filler(segment) is filler


def test_digest_link_with_only_pan_name_falls_back_to_excerpt():
    """「龙珠 完全版 漫画」：前文只有「UC」「?pwd=8888」的链接曾以此为标题挤进前 20。"""
    from pansearch.adapters.telegram import TelegramAdapter

    text = "龙珠完全版漫画 全42卷 夸克 https://pan.quark.cn/s/aaa0000001 UC https://drive.uc.cn/s/bbb0000001"
    hits = TelegramAdapter._to_hits([{"channel": "c", "msg_id": 1, "text": text,
                                      "links": [{"url": "https://drive.uc.cn/s/bbb0000001"}]}], "龙珠")
    assert "龙珠" in hits[0].title


# ---------------------------------------------------------------- Web 序列化
def test_serialize_reports_type_counts_over_all_results():
    from pansearch.webapp import _serialize

    res = build_resources([
        RawHit(source="t", kind="t", url="https://pan.quark.cn/s/aaaa00000001", title="a"),
        RawHit(source="t", kind="t", url="https://pan.quark.cn/s/aaaa00000002", title="b"),
        RawHit(source="t", kind="t", url="https://pan.baidu.com/s/1AbCdEfGh", title="c"),
    ])
    out = pipeline.SearchOutcome(keyword="x", resources=res)
    data = _serialize(out, limit=1)
    assert data["shown"] == 1 and data["total"] == 3
    assert data["type_counts"] == {"quark": 2, "baidu": 1}
