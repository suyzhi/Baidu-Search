"""Sonnet5 二轮评审的回归测试（离线）。

逐条锁住这 6 个问题，避免"改完没人报警、下次又复发"：
  1. 并发写 SQLite 没有 WAL / busy_timeout -> 立刻 database is locked
  2. Web UI 根本没有 --sfw 能力（两个 endpoint 都没接这个参数）
  3. 流式搜索每到一个源就把全量去重 + 打分重跑一遍
  4. Web 层的异常被静默吞掉，全项目没有一处 logging
  5. cfg.get(k) or default 把显式配置的 0 吃掉（relaxed_penalty / retries / deadline …）
  6. store.stale() 的死代码、magnet 归一化连 tr= tracker 一起截掉
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from pansearch import pipeline, webapp
from pansearch.config import cfg_bool, cfg_float, cfg_int
from pansearch.dedupe import build_resources
from pansearch.models import PanType, RawHit, Status, VerifyResult
from pansearch.normalize import normalize_url
from pansearch.score import score_resource
from pansearch.store import VerifyCache
from pansearch.verifiers import VerifierPool


# ------------------------------------------------------- 1. SQLite 并发写
def test_verify_cache_uses_wal_and_busy_timeout(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3")
    try:
        mode = str(cache.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode == "wal", "没有 WAL：读写会互相阻塞，并发搜索容易撞 database is locked"
        assert cache.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        cache.close()


def test_second_writer_waits_instead_of_failing(tmp_path):
    """两个连接（两个并发搜索请求 / Web + 定时复验）同时写，不能直接报 database is locked。"""
    path = tmp_path / "c.sqlite3"
    holder = VerifyCache(path)
    writer = VerifyCache(path)
    # holder 抢住写锁，模拟"另一个请求正在写"；writer 这时去写，有 busy_timeout 就该排队等
    holder.conn.execute("BEGIN IMMEDIATE")
    holder.conn.execute(
        "INSERT OR REPLACE INTO verify_cache (surl, status, checked_at)"
        " VALUES ('busy', 'alive', 0)"
    )
    result: dict = {}

    def write_from_other_connection():
        try:
            writer.put("magnet:?xt=urn:btih:AAAA", VerifyResult(status=Status.ALIVE))
            result["ok"] = True
        except Exception as exc:                       # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=write_from_other_connection)
    t.start()
    time.sleep(0.3)          # 让写方先撞上锁并进入等待
    holder.conn.commit()     # 释放写锁
    t.join(timeout=5)

    assert result.get("ok") is True, f"第二个连接写失败：{result.get('error')!r}"
    assert writer.get("magnet:?xt=urn:btih:AAAA") is not None
    holder.close()
    writer.close()


def test_stale_still_parses_keys_and_skips_garbage(tmp_path):
    """删掉 stale() 里那两行死代码之后，键解析行为必须和以前完全一致。"""
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=0)
    cache.put("baidu:1AbCdEf", VerifyResult(status=Status.ALIVE), pwd="1234")
    cache.put("magnet:?xt=urn:btih:AAAA", VerifyResult(status=Status.ALIVE))
    cache.conn.execute(
        "INSERT OR REPLACE INTO verify_cache (surl, status, checked_at)"
        " VALUES ('no-version-prefix', 'alive', 0)"
    )
    cache.conn.commit()

    entries = {e["key"]: e for e in cache.stale(older_than_hours=0)}
    assert entries["baidu:1AbCdEf"]["pwd"] == "1234"
    assert entries["magnet:?xt=urn:btih:AAAA"]["pwd"] is None
    assert "no-version-prefix" not in entries, "拆不出两段的旧键应跳过，而不是崩掉"
    cache.close()


# ------------------------------------------------------- 2. Web 端 sfw
def _client(monkeypatch, sink: dict, cache=None) -> TestClient:
    async def fake_search(kw, **kwargs):
        sink.update(kwargs)
        return pipeline.SearchOutcome(keyword=kw, resources=[])

    monkeypatch.setattr(webapp, "run_search", fake_search)
    # 不碰真实的验活缓存库
    monkeypatch.setattr(webapp, "shared_cache", lambda: cache)
    return TestClient(webapp.app)


def test_api_search_sfw_defaults_on_and_can_be_disabled(monkeypatch):
    seen: dict = {}
    client = _client(monkeypatch, seen)

    assert client.get("/api/search", params={"kw": "沙丘"}).status_code == 200
    assert seen["sfw"] is True, "Web 端默认必须过滤成人源"

    assert client.get("/api/search", params={"kw": "沙丘", "sfw": "false"}).status_code == 200
    assert seen["sfw"] is False, "关掉时也要真的能关掉"


def test_stream_api_exposes_sfw_and_reports_pruned_counts(monkeypatch):
    seen: dict = {}
    client = _client(monkeypatch, seen)

    resp = client.get("/api/search/stream", params={"kw": "沙丘", "sfw": "false"})
    assert resp.status_code == 200
    assert seen["sfw"] is False
    assert "complete" in resp.text
    # 过滤掉多少条要如实回报，前端才有东西显示
    assert "nsfw_pruned" in resp.text and "adult_pruned" in resp.text


def test_web_passes_shared_cache_into_search(monkeypatch):
    seen: dict = {}
    sentinel = object()
    client = _client(monkeypatch, seen, cache=sentinel)
    assert client.get("/api/search", params={"kw": "沙丘"}).status_code == 200
    assert seen["cache"] is sentinel, "每次请求新建/销毁 VerifyCache 正是并发写冲突的来源"


def test_web_lifespan_owns_one_shared_verify_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("pansearch.store.DEFAULT_DB", tmp_path / "index.sqlite3")
    monkeypatch.setattr(webapp, "_shared_cache", None)

    with TestClient(webapp.app) as client:
        assert client.get("/api/health").json() == {"ok": True}
        cache = webapp._shared_cache
        assert cache is not None
        assert webapp.shared_cache() is cache, "同一进程内必须复用同一条缓存连接"

    assert webapp._shared_cache is None, "关机时要关掉连接，不能泄漏"
    with pytest.raises(sqlite3.ProgrammingError):
        cache.conn.execute("SELECT COUNT(*) FROM verify_cache")


def test_index_html_exposes_sfw_toggle():
    """前端也要真的把这个开关接上，否则参数加了也没人口。"""
    html = webapp.INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="sfw"' in html
    assert "sfw: $('#sfw').checked" in html, "搜索请求里必须带上 sfw 参数"


def test_verifier_pool_reuses_external_cache(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3")
    pool = VerifierPool(cfg={"retries": 0, "timeout": 5}, cache=cache)
    assert pool.cache is cache
    assert pool._owns_cache is False, "外部传进来的缓存不能由池子关掉"
    cache.close()


# ------------------------------------------------------- 3. 流式预览节流
class _FakeAdapter:
    primary_only = False
    kind = "pansou"
    cfg: dict = {}

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def search(self, kw: str, client) -> list[RawHit]:
        self.calls += 1
        tag = f"{self.name}{self.calls}"
        return [
            RawHit(source=self.name, kind="pansou",
                   url=f"https://pan.baidu.com/s/1{tag}{i:02d}",
                   title=f"{kw} 资源 {i}")
            for i in range(5)
        ]


async def _fake_search(monkeypatch, frames: list, **kwargs):
    adapters = [_FakeAdapter(n) for n in ("a", "b", "c")]
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: adapters)

    async def on_progress(outcome):
        frames.append(outcome)

    outcome = await pipeline.search("三体 4K", do_verify=False, alive_only=False,
                                    on_progress=on_progress, **kwargs)
    return outcome, adapters


def test_preview_throttle_rules():
    throttle = pipeline.PreviewThrottle(min_interval=0.4, min_new_hits=25)
    assert throttle.should_emit(0.0, 10) is True        # 首帧：先显示
    throttle.mark(0.0, 10)
    assert throttle.should_emit(0.05, 12) is False      # 太快且增量太小
    assert throttle.should_emit(0.05, 40) is True       # 增量够大
    throttle.mark(0.05, 40)
    assert throttle.should_emit(0.10, 41) is False
    assert throttle.should_emit(0.60, 41) is True       # 间隔够久


async def test_streaming_preview_is_throttled(monkeypatch):
    frames: list = []
    outcome, adapters = await _fake_search(monkeypatch, frames)
    jobs = sum(a.calls for a in adapters)
    assert jobs >= 6

    assert len(frames) < jobs, "每个源一返回就重算全量去重+打分 = 评审说的 CPU 浪费"
    assert len(frames) >= 1
    # 节流丢的只是中间帧，不是结果：收尾的完整结果一条都不能少
    assert outcome.raw_hits == jobs * 5
    assert len(outcome.resources) == jobs * 5


async def test_sfw_resource_level_filter_reaches_final_result(monkeypatch):
    """sfw 必须一路传到收尾那次 _prepare：否则最后一帧预览和最终结果会不一致。"""
    monkeypatch.setattr(pipeline, "sources_config", lambda: {"nsfw_sources": ["b"]})
    frames: list = []
    outcome, _ = await _fake_search(monkeypatch, frames, sfw=True)

    assert outcome.nsfw_pruned > 0, "资源级兜底过滤没生效（sfw 没传到 _prepare）"
    assert all("b" not in res.sources for res in outcome.resources)


async def test_preview_throttle_can_be_disabled_via_config(monkeypatch):
    """节流参数可配；配成 0/1 就退回"每帧都推"的老行为。"""
    monkeypatch.setattr(pipeline, "sources_config",
                        lambda: {"preview_min_interval_seconds": 0,
                                 "preview_min_new_hits": 1})
    frames: list = []
    outcome, adapters = await _fake_search(monkeypatch, frames)
    assert len(frames) == sum(a.calls for a in adapters)


# ------------------------------------------------------- 4. 日志
def test_get_logger_namespaces_under_pansearch():
    from pansearch.logutil import get_logger, setup_logging

    setup_logging("INFO")
    assert get_logger("store").name == "pansearch.store"
    assert get_logger("pansearch.webapp").name == "pansearch.webapp"
    assert logging.getLogger("pansearch").handlers, "没有 handler 就等于日志进了黑洞"


def test_stream_endpoint_logs_swallowed_exception(monkeypatch, caplog):
    async def boom(kw, **kwargs):
        raise RuntimeError("打分阶段炸了")

    monkeypatch.setattr(webapp, "run_search", boom)
    monkeypatch.setattr(webapp, "shared_cache", lambda: None)
    client = TestClient(webapp.app)

    with caplog.at_level(logging.ERROR, logger="pansearch"):
        resp = client.get("/api/search/stream", params={"kw": "沙丘"})

    assert resp.status_code == 200
    assert "打分阶段炸了" in resp.text, "客户端仍应收到 error 事件"
    assert "打分阶段炸了" in caplog.text, "服务端必须留痕，不能只推给客户端就完事"
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ------------------------------------------------------- 5. 配置里的 0
def test_cfg_helpers_keep_explicit_zero():
    assert cfg_float({"retries": 0}, "retries", 2) == 0.0
    assert cfg_int({"deadline": 0}, "deadline", 15) == 0
    assert cfg_bool({"x": False}, "x", True) is False
    # 只有"没配"（None / 空串）才回落默认值
    assert cfg_float({}, "retries", 2) == 2.0
    assert cfg_float({"retries": None}, "retries", 2) == 2.0
    assert cfg_float({"retries": ""}, "retries", 2) == 2.0
    assert cfg_bool({}, "x", True) is True
    # 非法值兜底，不让一个坏配置把整个搜索搞崩
    assert cfg_float({"retries": "abc"}, "retries", 2) == 2.0


def test_relaxed_penalty_zero_really_zeroes_score():
    hit = RawHit(source="tg", kind="tg", url="https://pan.baidu.com/s/1AbCdEf",
                 title="完全不相关的标题", relaxed=True, query="补搜词")
    res = build_resources([hit])[0]
    assert res.from_primary is False

    zeroed = score_resource(res, "沙丘", {"relaxed_penalty": 0, "relevance_power": 1.0})
    assert zeroed == 0.0, "relaxed_penalty: 0 必须真的归零，不能被 or 0.5 吃掉"

    defaulted = score_resource(res, "沙丘", {"relevance_power": 1.0})
    assert defaulted > 0, "默认配置下同一条资源是有分的（说明上一条不是本来就 0）"


def test_btsearch_retries_zero_is_respected():
    from pansearch.adapters.btsearch import BtSearchAdapter

    adapter = BtSearchAdapter({"retries": 0, "concurrency": 4})
    assert adapter.retries == 0, "sources.yaml 里 btsearch 明确写着不重试"


# ------------------------------------------------------- 6. 磁力归一化
def test_magnet_normalize_drops_dn_but_keeps_trackers():
    raw = ("magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF"
           "&dn=%5BSubsPlease%5D+Three-Body+01+%281080p%29"
           "&tr=udp%3A%2F%2Ftracker.example%3A80")
    out = normalize_url(PanType.MAGNET, raw, None, None)
    assert "btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF" in out
    assert "dn=" not in out, "dn 是显示名，同一资源各来源都不一样，归一化时去掉"
    assert "tr=" in out, "dn 后面的 tracker 不能被一起截掉（原来是 split('&dn=')[0]）"


def test_magnet_normalize_without_dn_is_unchanged():
    raw = "magnet:?xt=urn:btih:AAAA&tr=udp%3A%2F%2Ftracker.example"
    assert normalize_url(PanType.MAGNET, raw, None, None) == raw


def test_magnet_normalize_keeps_param_that_merely_ends_with_dn():
    raw = "magnet:?xt=urn:btih:AAAA&xdn=1&tr=x"
    assert normalize_url(PanType.MAGNET, raw, None, None) == raw
