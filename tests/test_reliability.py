"""可靠性回归：不静默降级 / 实例故障切换 / 查询级缓存一致性。"""

from __future__ import annotations

import asyncio

import pytest

from pansearch import pipeline
from pansearch.adapters import pansou
from pansearch.adapters.base import Adapter
from pansearch.models import RawHit


def _hit(token: str, title: str = "沙丘 全集") -> RawHit:
    return RawHit(source="s", kind="pansou", url=f"https://pan.quark.cn/s/{token}", title=title)


class OkSource(Adapter):
    name = "ok"

    async def search(self, kw, client):
        return [_hit("ok0000000001")]


class BoomSource(Adapter):
    name = "boom"

    async def search(self, kw, client):
        raise RuntimeError("站点挂了")


class PartialSource(Adapter):
    name = "partial"

    async def search(self, kw, client):
        from pansearch.adapters.base import publish_hits

        publish_hits([_hit("partial000001")])
        raise RuntimeError("拿到一半就挂了")


async def test_source_report_marks_failed_source(monkeypatch):
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [OkSource(), BoomSource()])
    out = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)

    assert out.degraded == ["boom"], "失败的源必须被显式标出，不能静默少结果"
    assert out.source_report["boom"]["status"] == "error"
    assert out.source_report["ok"]["status"] == "ok"
    assert out.source_report["ok"]["hits"] == 1


async def test_partial_source_is_degraded_not_error(monkeypatch):
    """拿到了部分结果但同时报错的源，标 degraded —— 一次补搜超时不该显示成"挂了"。"""
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [PartialSource()])
    out = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)
    assert out.resources, "部分结果必须保留"
    assert out.source_report["partial"]["status"] == "degraded"
    assert out.degraded == ["partial"]


# ---------------------------------------------------------------- 查询级缓存
class CountingSource(Adapter):
    name = "counting"
    calls = 0

    async def search(self, kw, client):
        CountingSource.calls += 1
        return [_hit("count00000001")]


async def test_repeat_search_is_served_from_cache(monkeypatch):
    CountingSource.calls = 0
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [CountingSource()])

    first = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)
    second = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)

    assert CountingSource.calls == 1, "同词同参数第二次不该再打一遍全网"
    assert second.from_cache is True
    assert [r.url for r in second.resources] == [r.url for r in first.resources]


async def test_cache_does_not_freeze_failed_empty_results(monkeypatch):
    """源故障导致的空结果不能缓存 —— 否则一次抖动会被记成"确实没有"。"""
    calls = {"n": 0}

    class Flaky(Adapter):
        name = "flaky"

        async def search(self, kw, client):
            calls["n"] += 1
            raise RuntimeError("源挂了")

    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [Flaky()])
    first = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)
    second = await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)
    assert calls["n"] == 2
    assert first.from_cache is False and second.from_cache is False


async def test_cache_key_separates_flags(monkeypatch):
    CountingSource.calls = 0
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [CountingSource()])
    await pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False)
    await pipeline.search("沙丘", do_verify=False, alive_only=True, relax=False)
    assert CountingSource.calls == 2, "参数不同不能复用缓存"


# ---------------------------------------------------------------- PanSou 实例切换
async def test_pansou_failover_and_health_cache(monkeypatch):
    pansou._HEALTH.clear()
    adapter = pansou.PansouAdapter({"instances": ["http://dead", "http://live"], "retries": 0})
    calls: list[str] = []

    async def fake_query(client, url, kw):
        calls.append(url)
        if url == "http://dead":
            raise RuntimeError("connection refused")
        return {"data": {"merged_by_type": {}}}

    monkeypatch.setattr(adapter, "_query", fake_query)
    try:
        await adapter.search("沙丘", None)
        assert calls == ["http://dead", "http://live"]
        assert adapter.used_instance == "http://live"

        calls.clear()
        await adapter.search("沙丘", None)
        assert calls == ["http://live"], "刚失败的实例应被排到最后，不再每次白等一次超时"
    finally:
        pansou._HEALTH.clear()


async def test_pansou_all_instances_down_reports_instances(monkeypatch):
    pansou._HEALTH.clear()
    adapter = pansou.PansouAdapter({"instances": ["http://a", "http://b"], "retries": 0})

    async def fake_query(client, url, kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(adapter, "_query", fake_query)
    try:
        with pytest.raises(RuntimeError) as err:
            await adapter.search("沙丘", None)
        assert "http://a" in str(err.value) and "http://b" in str(err.value)
    finally:
        pansou._HEALTH.clear()


def test_search_cache_can_be_cleared():
    pipeline._QUERY_CACHE["x"] = (0.0, pipeline.SearchOutcome(keyword="x"))
    pipeline.clear_query_cache()
    assert pipeline._QUERY_CACHE == {}
